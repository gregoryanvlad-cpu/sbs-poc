"""
Secondary bot entrypoint (player gateway).
Run this file in a separate Railway service (kinoteka-player).
Required env vars in that service:
 - DATABASE_URL (reference to the shared Postgres)
 - PLAYER_BOT_TOKEN (or BOT_TOKEN)
 - OWNER_TG_ID (any digits; used by shared config loader)
 - MAIN_BOT_USERNAME (e.g. sbsconnect_bot)
 - PLAYER_RATE_LIMIT_PER_MINUTE (comma-separated)
 - REZKA_MIRROR (optional, default https://rezka.ag)
"""

from __future__ import annotations
import asyncio
import logging
import os
import subprocess
import sys
from datetime import datetime

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CommandStart
from aiogram.fsm.storage.memory import MemoryStorage

from app.core.logging import setup_logging
from app.core.config import settings
from app.db.session import init_engine, session_scope
from app.repo import get_subscription, get_content_request_by_token
from app.bot.ui import utcnow
from app.db.models import ContentRequest  # модель content_requests
from HdRezkaApi import HdRezkaApi  # парсер Rezka

from urllib.parse import urlparse, urlunparse

log = logging.getLogger(__name__)


def _normalize_rezka_url(url: str) -> str:
    """Подменяет домен в ссылке на Rezka на зеркало из env.

    HdRezkaApi (>=11) создаётся *для конкретной страницы* (url фильма/сериала),
    а не для зеркала. Поэтому зеркало надо вшивать в сам URL.
    """

    mirror = os.getenv("REZKA_MIRROR")
    if not mirror:
        return url

    try:
        src = urlparse(url)
        dst = urlparse(mirror)
        if not dst.scheme or not dst.netloc:
            return url
        return urlunparse((dst.scheme, dst.netloc, src.path, src.params, src.query, src.fragment))
    except Exception:
        return url


def _load_rezka(url: str) -> HdRezkaApi:
    """Создаёт объект HdRezkaApi для конкретного контента и валидирует ok."""

    normalized = _normalize_rezka_url(url)
    rezka_obj = HdRezkaApi(normalized)
    if not getattr(rezka_obj, "ok", True):
        # В библиотеке есть rezka.exception
        exc = getattr(rezka_obj, "exception", None)
        if exc:
            raise exc
        raise RuntimeError("HdRezkaApi returned ok=False")
    return rezka_obj

# Rate-limit cache (простой, в памяти)
rate_cache = {}  # user_id → (count, last_time)

router = Router()


def rate_limit_exceeded(user_id: int) -> bool:
    limit = int(os.getenv("PLAYER_RATE_LIMIT_PER_MINUTE", "15"))
    now = datetime.utcnow().timestamp()
    if user_id in rate_cache:
        count, last_time = rate_cache[user_id]
        if now - last_time < 60:
            if count >= limit:
                return True
            rate_cache[user_id] = (count + 1, last_time)
            return False
    rate_cache[user_id] = (1, now)
    return False


def _is_sub_active(end_at) -> bool:
    if not end_at:
        return False
    try:
        return end_at > utcnow()
    except Exception:
        return False


@router.message(CommandStart(deep_link=True))
async def handle_start_with_token(message: Message) -> None:
    """Обработка /start <token> из основного бота"""
    user_id = message.from_user.id
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Недействительная ссылка. Откройте фильм из основного бота.")
        return

    token = args[1].strip()

    if rate_limit_exceeded(user_id):
        await message.answer("Слишком много запросов. Подождите минуту.")
        return

    async with session_scope() as session:
        req = await get_content_request_by_token(session, token)
        if not req:
            await message.answer("Ссылка устарела или недействительна.")
            return

        url = req.content_url

        # Повторная проверка подписки
        sub = await get_subscription(session, user_id)
        if not _is_sub_active(sub.end_at):
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Купить подписку", url=f"t.me/{settings.MAIN_BOT_USERNAME}")]
            ])
            await message.answer(
                "У вас нет активной подписки. Оформите в основном боте:",
                reply_markup=kb
            )
            return

    # Парсинг контента из Rezka
    try:
        rezka_item = _load_rezka(url)

        title = getattr(rezka_item, "name", "Без названия")
        year = getattr(rezka_item, "releaseYear", None) or getattr(rezka_item, "year", None) or "—"
        poster = getattr(rezka_item, "thumbnail", None) or getattr(rezka_item, "thumbnailHQ", None)
        description = (getattr(rezka_item, "description", "Описание отсутствует") or "Описание отсутствует")[:600]

        episodes_info = getattr(rezka_item, "episodesInfo", None) or []
        is_series = bool(episodes_info)

        # Сериал — выбор сезона
        if is_series:
            kb = InlineKeyboardMarkup(inline_keyboard=[])
            seasons = [s.get("season") for s in episodes_info if isinstance(s, dict) and s.get("season") is not None]
            for season_num in sorted(set(int(s) for s in seasons)):
                kb.inline_keyboard.append([
                    InlineKeyboardButton(text=f"Сезон {season_num}", callback_data=f"season:{season_num}:{url}")
                ])
            text = f"<b>{title} ({year})</b>\n\n{description}\n\nВыберите сезон:"
            if poster:
                await message.answer_photo(photo=poster, caption=text, reply_markup=kb, parse_mode="HTML")
            else:
                await message.answer(text, reply_markup=kb, parse_mode="HTML")
            return

        # Фильм — сразу качества
        translators = getattr(rezka_item, "translators", None) or {}
        translation = None
        try:
            if isinstance(translators, dict) and translators:
                translation = next(iter(translators.keys()))
        except Exception:
            translation = None

        stream = rezka_item.getStream(translation=translation) if translation else rezka_item.getStream()
        videos = getattr(stream, "videos", {}) or {}

        kb = InlineKeyboardMarkup(inline_keyboard=[])
        for quality in videos.keys():
            try:
                link = stream(quality)
            except Exception:
                continue
            if link:
                kb.inline_keyboard.append([InlineKeyboardButton(text=str(quality), url=str(link))])

        text = f"<b>{title} ({year})</b>\n\n{description}"
        if poster:
            await message.answer_photo(photo=poster, caption=text, reply_markup=kb, parse_mode="HTML")
        else:
            await message.answer(text, reply_markup=kb, parse_mode="HTML")

    except Exception:
        log.exception(f"Ошибка обработки контента {url}")
        await message.answer("Не удалось загрузить контент. Попробуйте позже.")


@router.callback_query(F.data.startswith("season:"))
async def handle_season(callback: CallbackQuery) -> None:
    """Выбор сезона → список серий"""
    parts = callback.data.split(":", 2)
    if len(parts) < 3:
        await callback.answer("Ошибка данных.")
        return

    season_str = parts[1]
    url = parts[2]

    try:
        season = int(season_str)
        rezka_item = _load_rezka(url)
        episodes_info = getattr(rezka_item, "episodesInfo", None) or []

        episodes: list[int] = []
        for s in episodes_info:
            if not isinstance(s, dict):
                continue
            if int(s.get("season", -1)) != season:
                continue
            for ep in s.get("episodes", []) or []:
                if isinstance(ep, dict) and ep.get("episode") is not None:
                    episodes.append(int(ep["episode"]))

        kb = InlineKeyboardMarkup(inline_keyboard=[])
        for ep in sorted(set(episodes)):
            kb.inline_keyboard.append([
                InlineKeyboardButton(text=f"Серия {ep}", callback_data=f"episode:{season}:{ep}:{url}")
            ])

        await callback.message.edit_text(f"Сезон {season}: выберите серию", reply_markup=kb)
        await callback.answer()

    except Exception:
        log.exception("Ошибка обработки сезона")
        await callback.answer("Ошибка. Попробуйте позже.", show_alert=True)


@router.callback_query(F.data.startswith("episode:"))
async def handle_episode(callback: CallbackQuery) -> None:
    """Выбор серии → выбор озвучки"""
    parts = callback.data.split(":", 3)
    if len(parts) < 4:
        await callback.answer("Ошибка данных.")
        return

    season_str = parts[1]
    episode_str = parts[2]
    url = parts[3]

    try:
        season = int(season_str)
        episode = int(episode_str)
        rezka_item = _load_rezka(url)
        episodes_info = getattr(rezka_item, "episodesInfo", None) or []

        translations = []
        for s in episodes_info:
            if not isinstance(s, dict) or int(s.get("season", -1)) != season:
                continue
            for ep in s.get("episodes", []) or []:
                if not isinstance(ep, dict) or int(ep.get("episode", -1)) != episode:
                    continue
                translations = ep.get("translations", []) or []
                break

        kb = InlineKeyboardMarkup(inline_keyboard=[])
        for t in translations:
            if not isinstance(t, dict):
                continue
            trans_id = t.get("translator_id") or t.get("id")
            trans_name = t.get("translator_name") or t.get("name") or "Озвучка"
            if trans_id is None:
                continue
            kb.inline_keyboard.append([
                InlineKeyboardButton(text=str(trans_name), callback_data=f"trans:{season}:{episode}:{trans_id}:{url}")
            ])

        if not kb.inline_keyboard:
            kb.inline_keyboard.append([
                InlineKeyboardButton(text="По умолчанию", callback_data=f"trans:{season}:{episode}:None:{url}")
            ])

        await callback.message.edit_text(f"Серия {episode} (сезон {season}): выберите озвучку", reply_markup=kb)
        await callback.answer()

    except Exception:
        log.exception("Ошибка обработки серии")
        await callback.answer("Ошибка. Попробуйте позже.", show_alert=True)


@router.callback_query(F.data.startswith("trans:"))
async def handle_translator(callback: CallbackQuery) -> None:
    """Выбор озвучки → показ качеств"""
    parts = callback.data.split(":", 4)
    if len(parts) < 5:
        await callback.answer("Ошибка данных.")
        return

    season_str = parts[1]
    episode_str = parts[2]
    trans_id = parts[3]
    url = parts[4]

    try:
        season = int(season_str)
        episode = int(episode_str)
        rezka_item = _load_rezka(url)

        translation = None if trans_id in {"None", "none", "null", ""} else trans_id
        stream = rezka_item.getStream(season, episode, translation=translation)
        videos = getattr(stream, "videos", {}) or {}

        kb = InlineKeyboardMarkup(inline_keyboard=[])
        for quality in videos.keys():
            try:
                link = stream(quality)
            except Exception:
                continue
            if link:
                kb.inline_keyboard.append([InlineKeyboardButton(text=str(quality), url=str(link))])

        await callback.message.edit_text("Выберите качество:", reply_markup=kb)
        await callback.answer()

    except Exception:
        log.exception("Ошибка обработки озвучки")
        await callback.answer("Ошибка. Попробуйте позже.", show_alert=True)


def _run_alembic_upgrade_head_best_effort() -> None:
    """Apply migrations at boot (best-effort)."""
    try:
        subprocess.check_call([sys.executable, "-m", "alembic", "upgrade", "head"])
        log.info("✅ Alembic migrations applied: upgrade head")
    except Exception:
        # best-effort; do not crash player bot
        log.exception("❌ Alembic upgrade head failed. Continuing without migrations.")


async def main() -> None:
    setup_logging()
    init_engine(settings.database_url)
    _run_alembic_upgrade_head_best_effort()

    bot = Bot(token=settings.bot_token)
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)
    dp.include_router(router)

    log.info("🚀 Player bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
