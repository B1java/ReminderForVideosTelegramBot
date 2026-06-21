from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, NetworkError, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)


load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "408141472"))
REMINDER_HOUR = int(os.getenv("REMINDER_HOUR", "10"))
REMINDER_MINUTE = int(os.getenv("REMINDER_MINUTE", "0"))
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow"))
PROFILES_FILE = Path(os.getenv("PROFILES_FILE", "data/profiles.txt"))
NETWORK_ERRORS_FILE = Path(os.getenv("NETWORK_ERRORS_FILE", "data/network_errors.txt"))
POLLING_TIMEOUT = int(os.getenv("POLLING_TIMEOUT", "30"))
POLLING_READ_TIMEOUT = float(os.getenv("POLLING_READ_TIMEOUT", "45"))
POLLING_CONNECT_TIMEOUT = float(os.getenv("POLLING_CONNECT_TIMEOUT", "10"))
POLLING_POOL_TIMEOUT = float(os.getenv("POLLING_POOL_TIMEOUT", "10"))

NAME_MAX_LENGTH = 100
TOTAL_MIN = 0
TOTAL_MAX = 100
PER_DAY_MIN = 0
PER_DAY_MAX = 15
CHAT_ID_RE = re.compile(r"^-\d+$")
NETWORK_ERROR_WINDOW = timedelta(hours=12)
NETWORK_ERROR_THRESHOLD = 3

(
    CREATE_NAME,
    CREATE_CHATS,
    CREATE_PER_DAY,
    EDIT_NAME,
    EDIT_CHATS,
    EDIT_TOTAL,
    EDIT_PER_DAY,
) = range(7)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Profile:
    name: str
    chat_ids: tuple[str, ...]
    total_videos: int
    videos_per_day: int

    @classmethod
    def parse(cls, line: str) -> "Profile":
        parts = line.strip().split(":")
        if len(parts) != 4:
            raise ValueError("profile line must have 4 colon-separated parts")

        name, chats, total, per_day = parts
        profile = cls(
            name=validate_profile_name(name),
            chat_ids=parse_chat_ids(chats),
            total_videos=validate_int_range(total, "total_videos_available", TOTAL_MIN, TOTAL_MAX),
            videos_per_day=validate_int_range(raw=per_day, field_name="videos_per_day", min_value=PER_DAY_MIN, max_value=PER_DAY_MAX),
        )
        return profile

    def serialize(self) -> str:
        chats = ",".join(self.chat_ids)
        return f"{self.name}:{chats}:{self.total_videos}:{self.videos_per_day}"

    def with_name(self, name: str) -> "Profile":
        return Profile(name, self.chat_ids, self.total_videos, self.videos_per_day)

    def with_chats(self, chat_ids: Iterable[str]) -> "Profile":
        return Profile(self.name, tuple(chat_ids), self.total_videos, self.videos_per_day)

    def with_total(self, total: int) -> "Profile":
        return Profile(self.name, self.chat_ids, total, self.videos_per_day)

    def with_per_day(self, per_day: int) -> "Profile":
        return Profile(self.name, self.chat_ids, self.total_videos, per_day)


class ProfileStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()

    async def list(self) -> list[Profile]:
        async with self._lock:
            return self._read_unlocked()

    async def get(self, name: str) -> Profile | None:
        profiles = await self.list()
        return next((profile for profile in profiles if profile.name == name), None)

    async def get_by_index(self, index: int) -> Profile | None:
        profiles = await self.list()
        if index < 0 or index >= len(profiles):
            return None
        return profiles[index]

    async def get_index(self, name: str) -> int | None:
        profiles = await self.list()
        return self._find_index(profiles, name)

    async def create(self, profile: Profile) -> None:
        async with self._lock:
            profiles = self._read_unlocked()
            if self._find_index(profiles, profile.name) is not None:
                raise ValueError("профиль с таким названием уже существует")
            profiles.append(profile)
            self._write_unlocked(profiles)

    async def replace(self, old_name: str, profile: Profile) -> None:
        async with self._lock:
            profiles = self._read_unlocked()
            index = self._find_index(profiles, old_name)
            if index is None:
                raise ValueError("профиль не найден")

            duplicate_index = self._find_index(profiles, profile.name)
            if duplicate_index is not None and duplicate_index != index:
                raise ValueError("профиль с таким названием уже существует")

            profiles[index] = profile
            self._write_unlocked(profiles)

    async def delete(self, name: str) -> bool:
        async with self._lock:
            profiles = self._read_unlocked()
            filtered = [profile for profile in profiles if profile.name != name]
            if len(filtered) == len(profiles):
                return False
            self._write_unlocked(filtered)
            return True

    async def spend_day(self) -> list[tuple[Profile, int]]:
        async with self._lock:
            profiles = self._read_unlocked()
            reminders: list[tuple[Profile, int]] = []
            updated: list[Profile] = []

            for profile in profiles:
                remaining = max(0, profile.total_videos - profile.videos_per_day)
                if profile.videos_per_day > 0 and remaining < profile.videos_per_day:
                    reminders.append((profile, remaining))
                updated.append(profile.with_total(remaining))

            self._write_unlocked(updated)
            return reminders

    def _read_unlocked(self) -> list[Profile]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()
            return []

        profiles: list[Profile] = []
        for line_number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                profiles.append(Profile.parse(line))
            except ValueError as exc:
                logger.warning("Skipping malformed profile line %s: %s", line_number, exc)
        return profiles

    def _write_unlocked(self, profiles: list[Profile]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        content = "\n".join(profile.serialize() for profile in profiles)
        if content:
            content += "\n"

        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent, delete=False) as temp_file:
            temp_file.write(content)
            temp_name = temp_file.name

        Path(temp_name).replace(self.path)

    @staticmethod
    def _find_index(profiles: list[Profile], name: str) -> int | None:
        for index, profile in enumerate(profiles):
            if profile.name == name:
                return index
        return None


store = ProfileStore(PROFILES_FILE)


class NetworkErrorStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()

    async def add(self, occurred_at: datetime) -> int:
        async with self._lock:
            timestamps = self._read_unlocked()
            timestamps.append(occurred_at)
            recent = self._recent(timestamps, occurred_at)
            self._write_unlocked(recent)
            return len(recent)

    async def count_recent(self, now: datetime) -> int:
        async with self._lock:
            recent = self._recent(self._read_unlocked(), now)
            self._write_unlocked(recent)
            return len(recent)

    async def clear_recent(self, now: datetime) -> None:
        async with self._lock:
            cutoff = now - NETWORK_ERROR_WINDOW
            remaining = [timestamp for timestamp in self._read_unlocked() if timestamp < cutoff]
            self._write_unlocked(remaining)

    def _read_unlocked(self) -> list[datetime]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()
            return []

        timestamps: list[datetime] = []
        for line_number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                timestamp = datetime.fromisoformat(line)
            except ValueError:
                logger.warning("Skipping malformed network error line %s", line_number)
                continue
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=TIMEZONE)
            timestamps.append(timestamp)
        return timestamps

    def _write_unlocked(self, timestamps: list[datetime]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        content = "\n".join(timestamp.isoformat() for timestamp in timestamps)
        if content:
            content += "\n"

        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent, delete=False) as temp_file:
            temp_file.write(content)
            temp_name = temp_file.name

        Path(temp_name).replace(self.path)

    @staticmethod
    def _recent(timestamps: list[datetime], now: datetime) -> list[datetime]:
        cutoff = now - NETWORK_ERROR_WINDOW
        return [timestamp for timestamp in timestamps if timestamp >= cutoff]


network_error_store = NetworkErrorStore(NETWORK_ERRORS_FILE)


def validate_profile_name(raw: str) -> str:
    name = raw.strip()
    if not 1 <= len(name) <= NAME_MAX_LENGTH:
        raise ValueError("название профиля должно быть длиной 1-100 символов")
    if ":" in name:
        raise ValueError("название профиля не может содержать ':'")
    return name


def parse_chat_ids(raw: str) -> tuple[str, ...]:
    chat_ids = tuple(chat_id.strip() for chat_id in raw.split(",") if chat_id.strip())
    if not chat_ids:
        raise ValueError("укажите хотя бы один chat_id")

    invalid = [chat_id for chat_id in chat_ids if not CHAT_ID_RE.fullmatch(chat_id)]
    if invalid:
        raise ValueError("chat_id должен быть отрицательным числом, например -5225157392")

    return chat_ids


def validate_int_range(raw: str, field_name: str, min_value: int, max_value: int) -> int:
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{field_name} должен быть целым числом") from exc

    if value < min_value or value > max_value:
        raise ValueError(f"{field_name} должен быть в диапазоне {min_value}-{max_value}")
    return value


def is_admin(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id == ADMIN_USER_ID)


async def reject_non_admin(update: Update) -> bool:
    if is_admin(update):
        return False
    if update.callback_query:
        await update.callback_query.answer("Нет доступа.", show_alert=True)
    user = update.effective_user
    logger.info("Rejected user id=%s", user.id if user else None)
    return True


def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if await reject_non_admin(update):
            return ConversationHandler.END
        return await func(update, context)

    return wrapper


def main_menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Создать профиль", callback_data="create")],
            [InlineKeyboardButton("Профили", callback_data="profiles")],
            [InlineKeyboardButton("Проверить сейчас", callback_data="check_now")],
        ]
    )


def profile_markup(profile_index: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Изменить название", callback_data=f"edit_name:{profile_index}")],
            [InlineKeyboardButton("Изменить чаты", callback_data=f"edit_chats:{profile_index}")],
            [InlineKeyboardButton("Изменить видео", callback_data=f"edit_total:{profile_index}")],
            [InlineKeyboardButton("Изменить видео в день", callback_data=f"edit_per_day:{profile_index}")],
            [InlineKeyboardButton("Удалить", callback_data=f"delete:{profile_index}")],
            [InlineKeyboardButton("Назад", callback_data="profiles")],
        ]
    )


def cancel_edit_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("Отменить", callback_data="cancel_edit")]])


def delete_confirmation_markup(profile_index: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Да, удалить", callback_data=f"confirm_delete:{profile_index}")],
            [InlineKeyboardButton("Отменить", callback_data=f"cancel_delete:{profile_index}")],
        ]
    )


async def reply_or_edit(update: Update, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
        except BadRequest as exc:
            if "Message is not modified" not in str(exc):
                raise
            logger.debug("Skipped unchanged callback message edit")
    elif update.effective_message:
        await update.effective_message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)


def format_profile(profile: Profile) -> str:
    chats = ", ".join(profile.chat_ids)
    if profile.videos_per_day == 0:
        days_left = "расход 0"
    else:
        days_left = str(profile.total_videos // profile.videos_per_day)

    return (
        f"<b>{html.escape(profile.name)}</b>\n"
        f"Чаты: <code>{html.escape(chats)}</code>\n"
        f"Видео доступно: <b>{profile.total_videos}</b>\n"
        f"Видео в день: <b>{profile.videos_per_day}</b>\n"
        f"Полных дней публикаций: <b>{days_left}</b>"
    )


@admin_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await reply_or_edit(update, "Меню управления профилями:", main_menu_markup())
    return ConversationHandler.END


@admin_only
async def show_profiles(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()

    profiles = await store.list()
    if not profiles:
        await reply_or_edit(update, "Профилей пока нет.", main_menu_markup())
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton(profile.name, callback_data=f"profile:{index}")]
        for index, profile in enumerate(profiles)
    ]
    keyboard.append([InlineKeyboardButton("Назад", callback_data="menu")])
    await reply_or_edit(update, "Выберите профиль:", InlineKeyboardMarkup(keyboard))
    return ConversationHandler.END


@admin_only
async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()

    index = int(update.callback_query.data.split(":", 1)[1])
    profile = await store.get_by_index(index)
    if not profile:
        await reply_or_edit(update, "Профиль не найден.", main_menu_markup())
        return ConversationHandler.END

    context.user_data["profile_name"] = profile.name
    await reply_or_edit(update, format_profile(profile), profile_markup(index))
    return ConversationHandler.END


@admin_only
async def create_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
    context.user_data.clear()
    await reply_or_edit(update, "Введите название профиля длиной 1-100 символов:")
    return CREATE_NAME


@admin_only
async def create_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["new_name"] = validate_profile_name(update.effective_message.text)
    except ValueError as exc:
        await reply_or_edit(update, f"Ошибка: {exc}\nВведите название еще раз:")
        return CREATE_NAME

    await reply_or_edit(update, "Введите chat_id или несколько chat_id через запятую. Пример: -5225157392")
    return CREATE_CHATS


@admin_only
async def create_chats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["new_chats"] = parse_chat_ids(update.effective_message.text)
    except ValueError as exc:
        await reply_or_edit(update, f"Ошибка: {exc}\nВведите chat_id еще раз:")
        return CREATE_CHATS

    await reply_or_edit(update, "Введите сколько видео публикуется в день. Диапазон: 0-15")
    return CREATE_PER_DAY


@admin_only
async def create_per_day(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        per_day = validate_int_range(update.effective_message.text, "videos_per_day", PER_DAY_MIN, PER_DAY_MAX)
        profile = Profile(
            name=context.user_data["new_name"],
            chat_ids=context.user_data["new_chats"],
            total_videos=0,
            videos_per_day=per_day,
        )
        await store.create(profile)
    except ValueError as exc:
        await reply_or_edit(update, f"Ошибка: {exc}\nВведите значение еще раз:")
        return CREATE_PER_DAY

    context.user_data.clear()
    await reply_or_edit(update, "Профиль создан. Количество видео по умолчанию: 0.", main_menu_markup())
    return ConversationHandler.END


async def get_selected_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Profile | None:
    callback_data = update.callback_query.data if update.callback_query else ""
    if ":" in callback_data:
        index = int(callback_data.split(":", 1)[1])
        profile = await store.get_by_index(index)
        if profile:
            context.user_data["profile_name"] = profile.name
            return profile

    name = context.user_data.get("profile_name")
    if not name:
        return None
    return await store.get(name)


@admin_only
async def cancel_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()

    name = context.user_data.get("profile_name")
    if not name:
        await reply_or_edit(update, "Профиль не найден.", main_menu_markup())
        return ConversationHandler.END

    profile = await store.get(name)
    index = await store.get_index(name)
    if not profile or index is None:
        await reply_or_edit(update, "Профиль не найден.", main_menu_markup())
        return ConversationHandler.END

    await reply_or_edit(update, format_profile(profile), profile_markup(index))
    return ConversationHandler.END


@admin_only
async def edit_name_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
    profile = await get_selected_profile(update, context)
    if not profile:
        await reply_or_edit(update, "Профиль не найден.", main_menu_markup())
        return ConversationHandler.END
    await reply_or_edit(
        update,
        f"Текущее название: <b>{html.escape(profile.name)}</b>\nВведите новое название:",
        cancel_edit_markup(),
    )
    return EDIT_NAME


@admin_only
async def edit_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    profile = await get_selected_profile(update, context)
    if not profile:
        await reply_or_edit(update, "Профиль не найден.", main_menu_markup())
        return ConversationHandler.END

    try:
        new_name = validate_profile_name(update.effective_message.text)
        await store.replace(profile.name, profile.with_name(new_name))
    except ValueError as exc:
        await reply_or_edit(update, f"Ошибка: {exc}\nВведите название еще раз:", cancel_edit_markup())
        return EDIT_NAME

    context.user_data["profile_name"] = new_name
    await reply_or_edit(update, "Название обновлено.", main_menu_markup())
    return ConversationHandler.END


@admin_only
async def edit_chats_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
    profile = await get_selected_profile(update, context)
    if not profile:
        await reply_or_edit(update, "Профиль не найден.", main_menu_markup())
        return ConversationHandler.END
    await reply_or_edit(
        update,
        f"Текущие чаты: <code>{html.escape(','.join(profile.chat_ids))}</code>\nВведите новый список через запятую:",
        cancel_edit_markup(),
    )
    return EDIT_CHATS


@admin_only
async def edit_chats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    profile = await get_selected_profile(update, context)
    if not profile:
        await reply_or_edit(update, "Профиль не найден.", main_menu_markup())
        return ConversationHandler.END

    try:
        chat_ids = parse_chat_ids(update.effective_message.text)
        await store.replace(profile.name, profile.with_chats(chat_ids))
    except ValueError as exc:
        await reply_or_edit(update, f"Ошибка: {exc}\nВведите chat_id еще раз:", cancel_edit_markup())
        return EDIT_CHATS

    await reply_or_edit(update, "Чаты обновлены.", main_menu_markup())
    return ConversationHandler.END


@admin_only
async def edit_total_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
    profile = await get_selected_profile(update, context)
    if not profile:
        await reply_or_edit(update, "Профиль не найден.", main_menu_markup())
        return ConversationHandler.END
    await reply_or_edit(
        update,
        f"Сейчас видео: <b>{profile.total_videos}</b>\nВведите новое значение 0-100:",
        cancel_edit_markup(),
    )
    return EDIT_TOTAL


@admin_only
async def edit_total(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    profile = await get_selected_profile(update, context)
    if not profile:
        await reply_or_edit(update, "Профиль не найден.", main_menu_markup())
        return ConversationHandler.END

    try:
        total = validate_int_range(update.effective_message.text, "total_videos_available", TOTAL_MIN, TOTAL_MAX)
        await store.replace(profile.name, profile.with_total(total))
    except ValueError as exc:
        await reply_or_edit(update, f"Ошибка: {exc}\nВведите значение еще раз:", cancel_edit_markup())
        return EDIT_TOTAL

    await reply_or_edit(update, "Количество видео обновлено.", main_menu_markup())
    return ConversationHandler.END


@admin_only
async def edit_per_day_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
    profile = await get_selected_profile(update, context)
    if not profile:
        await reply_or_edit(update, "Профиль не найден.", main_menu_markup())
        return ConversationHandler.END
    await reply_or_edit(
        update,
        f"Сейчас видео в день: <b>{profile.videos_per_day}</b>\nВведите новое значение 0-15:",
        cancel_edit_markup(),
    )
    return EDIT_PER_DAY


@admin_only
async def edit_per_day(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    profile = await get_selected_profile(update, context)
    if not profile:
        await reply_or_edit(update, "Профиль не найден.", main_menu_markup())
        return ConversationHandler.END

    try:
        per_day = validate_int_range(update.effective_message.text, "videos_per_day", PER_DAY_MIN, PER_DAY_MAX)
        await store.replace(profile.name, profile.with_per_day(per_day))
    except ValueError as exc:
        await reply_or_edit(update, f"Ошибка: {exc}\nВведите значение еще раз:", cancel_edit_markup())
        return EDIT_PER_DAY

    await reply_or_edit(update, "Количество видео в день обновлено.", main_menu_markup())
    return ConversationHandler.END


@admin_only
async def delete_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
    profile = await get_selected_profile(update, context)
    if not profile:
        await reply_or_edit(update, "Профиль не найден.", main_menu_markup())
        return ConversationHandler.END

    index = await store.get_index(profile.name)
    if index is None:
        await reply_or_edit(update, "Профиль не найден.", main_menu_markup())
        return ConversationHandler.END

    await reply_or_edit(
        update,
        f"Удалить профиль <b>{html.escape(profile.name)}</b>?",
        delete_confirmation_markup(index),
    )
    context.user_data["pending_delete_profile_name"] = profile.name
    return ConversationHandler.END


@admin_only
async def cancel_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()

    profile = await get_selected_profile(update, context)
    if not profile:
        await reply_or_edit(update, "Профиль не найден.", main_menu_markup())
        return ConversationHandler.END

    index = await store.get_index(profile.name)
    if index is None:
        await reply_or_edit(update, "Профиль не найден.", main_menu_markup())
        return ConversationHandler.END

    context.user_data.pop("pending_delete_profile_name", None)
    await reply_or_edit(update, format_profile(profile), profile_markup(index))
    return ConversationHandler.END


@admin_only
async def confirm_delete_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()

    profile = await get_selected_profile(update, context)
    if not profile:
        await reply_or_edit(update, "Профиль не найден.", main_menu_markup())
        return ConversationHandler.END

    if context.user_data.get("pending_delete_profile_name") != profile.name:
        await reply_or_edit(update, "Подтверждение удаления устарело.", main_menu_markup())
        return ConversationHandler.END

    await store.delete(profile.name)
    context.user_data.pop("pending_delete_profile_name", None)
    context.user_data.pop("profile_name", None)
    await reply_or_edit(update, "Профиль удален.", main_menu_markup())
    return ConversationHandler.END


async def send_reminder(application: Application, chat_id: int | str, text: str) -> str | None:
    try:
        await application.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)
    except (BadRequest, Forbidden) as exc:
        logger.warning("Cannot send reminder to chat_id=%s: %s", chat_id, exc)
        return str(exc)
    except TelegramError as exc:
        logger.warning("Telegram error while sending reminder to chat_id=%s: %s", chat_id, exc)
        return str(exc)
    except Exception as exc:
        logger.exception("Unexpected error while sending reminder to chat_id=%s", chat_id)
        return str(exc)
    return None


async def run_daily_check(application: Application, *, reply_to_admin: bool = False) -> None:
    reminders = await store.spend_day()

    if not reminders and reply_to_admin:
        await send_reminder(application, ADMIN_USER_ID, "Проверка выполнена. Напоминания не нужны.")
        return

    for profile, remaining_after_today in reminders:
        available_tomorrow = (
            profile.total_videos
            if profile.total_videos < profile.videos_per_day
            else remaining_after_today
        )
        text = (
            f"В профиле <b>{html.escape(profile.name)}</b> заканчиваются видео.\n"
            f"На следующий день доступно <b>{available_tomorrow}</b> видео, "
            f"а нужно <b>{profile.videos_per_day}</b>."
        )

        await send_reminder(application, ADMIN_USER_ID, text)
        for chat_id in profile.chat_ids:
            error = await send_reminder(application, chat_id, text)
            if error:
                await send_reminder(
                    application,
                    ADMIN_USER_ID,
                    (
                        f"Не удалось отправить напоминание в чат <code>{html.escape(str(chat_id))}</code> "
                        f"для профиля <b>{html.escape(profile.name)}</b>.\n"
                        f"Telegram ответил: <code>{html.escape(error)}</code>"
                    ),
                )

    if reply_to_admin:
        await send_reminder(application, ADMIN_USER_ID, "Проверка выполнена.")


@admin_only
async def check_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
    await run_daily_check(context.application, reply_to_admin=True)
    await reply_or_edit(update, "Проверка запущена. Результат отправлен админу в личные сообщения.", main_menu_markup())
    return ConversationHandler.END


async def scheduled_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    await run_daily_check(context.application)


def is_polling_read_error(error: object) -> bool:
    if not isinstance(error, NetworkError):
        return False

    current: BaseException | None = error
    while current:
        error_type = type(current)
        if error_type.__name__ == "ReadError" and error_type.__module__.split(".", maxsplit=1)[0] in {"httpx", "httpcore"}:
            return True
        current = current.__cause__ or current.__context__

    return "ReadError" in str(error)


async def network_error_report_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    now = datetime.now(TIMEZONE)
    count = await network_error_store.count_recent(now)
    if count < NETWORK_ERROR_THRESHOLD:
        return

    error = await send_reminder(
        context.application,
        ADMIN_USER_ID,
        (
            "За последние 12 часов бот несколько раз терял соединение с Telegram API.\n"
            f"Количество сетевых ошибок чтения: <b>{count}</b>."
        ),
    )
    if error is None:
        await network_error_store.clear_recent(now)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_polling_read_error(context.error):
        count = await network_error_store.add(datetime.now(TIMEZONE))
        logger.warning("Telegram polling read error recorded: %s (recent_count=%s)", context.error, count)
        return

    logger.exception("Unhandled bot error", exc_info=context.error)
    try:
        await context.bot.send_message(ADMIN_USER_ID, "В боте произошла ошибка. Детали записаны в лог.")
    except Exception:
        logger.exception("Failed to notify admin about bot error")


def build_application() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set. Create .env from .env.example and set BOT_TOKEN.")

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .get_updates_read_timeout(POLLING_READ_TIMEOUT)
        .get_updates_connect_timeout(POLLING_CONNECT_TIMEOUT)
        .get_updates_pool_timeout(POLLING_POOL_TIMEOUT)
        .build()
    )

    conversation = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("menu", start),
            CallbackQueryHandler(start, pattern="^menu$"),
            CallbackQueryHandler(create_start, pattern="^create$"),
            CallbackQueryHandler(show_profiles, pattern="^profiles$"),
            CallbackQueryHandler(show_profile, pattern=r"^profile:\d+$"),
            CallbackQueryHandler(edit_name_start, pattern=r"^edit_name:\d+$"),
            CallbackQueryHandler(edit_chats_start, pattern=r"^edit_chats:\d+$"),
            CallbackQueryHandler(edit_total_start, pattern=r"^edit_total:\d+$"),
            CallbackQueryHandler(edit_per_day_start, pattern=r"^edit_per_day:\d+$"),
            CallbackQueryHandler(delete_profile, pattern=r"^delete:\d+$"),
            CallbackQueryHandler(confirm_delete_profile, pattern=r"^confirm_delete:\d+$"),
            CallbackQueryHandler(cancel_delete, pattern=r"^cancel_delete:\d+$"),
            CallbackQueryHandler(check_now, pattern="^check_now$"),
        ],
        states={
            CREATE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_name)],
            CREATE_CHATS: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_chats)],
            CREATE_PER_DAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_per_day)],
            EDIT_NAME: [
                CallbackQueryHandler(cancel_edit, pattern="^cancel_edit$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_name),
            ],
            EDIT_CHATS: [
                CallbackQueryHandler(cancel_edit, pattern="^cancel_edit$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_chats),
            ],
            EDIT_TOTAL: [
                CallbackQueryHandler(cancel_edit, pattern="^cancel_edit$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_total),
            ],
            EDIT_PER_DAY: [
                CallbackQueryHandler(cancel_edit, pattern="^cancel_edit$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_per_day),
            ],
        },
        fallbacks=[
            CommandHandler("start", start),
            CommandHandler("menu", start),
        ],
        allow_reentry=True,
    )

    application.add_handler(conversation)
    application.add_handler(CommandHandler("profiles", show_profiles))
    application.add_handler(CommandHandler("check_now", check_now))
    application.add_error_handler(on_error)
    application.job_queue.run_daily(
        scheduled_check,
        time=time(hour=REMINDER_HOUR, minute=REMINDER_MINUTE, tzinfo=TIMEZONE),
        name="daily-video-reminder",
    )
    application.job_queue.run_daily(
        network_error_report_check,
        time=time(hour=10, minute=0, tzinfo=TIMEZONE),
        name="morning-network-error-report",
    )
    application.job_queue.run_daily(
        network_error_report_check,
        time=time(hour=22, minute=0, tzinfo=TIMEZONE),
        name="evening-network-error-report",
    )
    return application


def main() -> None:
    application = build_application()
    logger.info("Bot started. Profiles file: %s", PROFILES_FILE)
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        timeout=POLLING_TIMEOUT,
        read_timeout=POLLING_READ_TIMEOUT,
        connect_timeout=POLLING_CONNECT_TIMEOUT,
        pool_timeout=POLLING_POOL_TIMEOUT,
    )


if __name__ == "__main__":
    main()
