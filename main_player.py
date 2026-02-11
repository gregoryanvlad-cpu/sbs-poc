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
import json
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
from app.db.models.user import User  # если нужно, подставь свою модель
from app.repo import get_content_request, get_subscription  # твои репо
from app.bot.ui import utcnow  # твоя утилита
from HdRezkaApi import HdRezkaApi  # парсер Rezka

log = logging.getLogger(__name__)

# Инициализация парсера Rezka
rezka = HdRezkaApi(mirror=os.getenv("REZKA_MIRROR", "https://rezka.ag"))

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

    # Достаем content_url по токену
    async with session_scope() as session:
        req = await get_content_request(session, token)
        if not req or req.expires_at < utcnow():
            await message.answer("Ссылка устарела или недействительна.")
            return

        # Проверка подписки
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

        url = req.content_url

    # Парсинг контента из Rezka
    try:
        item = rezka.get(url)
        if not item:
            await message.answer("Контент не найден или недоступен.")
            return

        title = item.title
        year = item.year or "—"
        poster = item.poster
        description = getattr(item, 'description', 'Описание отсутствует')[:600]

        # Если сериал — выбор сезона
        if hasattr(item, 'seasons') and item.seasons:
            kb = InlineKeyboardMarkup(inline_keyboard=[])
            for season_num in sorted(item.seasons.keys()):
                kb.inline_keyboard.append([
                    InlineKeyboardButton(text=f"Сезон {season_num}", callback_data=f"season:{season_num}:{url}")
                ])
            text = f"<b>{title} ({year})</b>\n\n{description}\n\nВыберите сезон:"
            if poster:
                await message.answer_photo(photo=poster, caption=text, reply_markup=kb, parse_mode="HTML")
            else:
                await message.answer(text, reply_markup=kb, parse_mode="HTML")

        # Если фильм — сразу качества
        else:
            streams = item.videos if hasattr(item, 'videos') else {}
            if not streams and hasattr(item, 'player'):
                streams = {"Смотреть": item.player}

            kb = InlineKeyboardMarkup(inline_keyboard=[])
            for quality, link in streams.items():
                kb.inline_keyboard.append([
                    InlineKeyboardButton(text=quality, url=link)
                ])

            text = f"<b>{title} ({year})</b>\n\n{description}"
            if poster:
                await message.answer_photo(photo=poster, caption=text, reply_markup=kb, parse_mode="HTML")
            else:
                await message.answer(text, reply_markup=kb, parse_mode="HTML")

    except Exception as e:
        log.exception(f"Ошибка обработки контента {url}")
        await message.answer("Не удалось загрузить контент. Попробуйте позже.")


@router.callback_query(F.data.startswith("season:"))
async def handle_season(callback: CallbackQuery) -> None:
    """Выбор сезона → список серий"""
    parts = callback.data.split(":")
    if len(parts) < 3:
        await callback.answer("Ошибка данных.")
        return

    season_str = parts[1]
    url = ":".join(parts[2:])  # на случай, если url с :

    try:
        season = int(season_str)
        item = rezka.get(url)
        episodes = item.seasons.get(season, {}).get('episodes', []) if hasattr(item, 'seasons') else []

        kb = InlineKeyboardMarkup(inline_keyboard=[])
        for ep in sorted(episodes):
            kb.inline_keyboard.append([
                InlineKeyboardButton(text=f"Серия {ep}", callback_data=f"episode:{season}:{ep}:{url}")
            ])

        await callback.message.edit_text(f"Сезон {season}: выберите серию", reply_markup=kb)
        await callback.answer()

    except Exception as e:
        log.exception("Ошибка обработки сезона")
        await callback.answer("Ошибка. Попробуйте позже.", show_alert=True)


@router.callback_query(F.data.startswith("episode:"))
async def handle_episode(callback: CallbackQuery) -> None:
    """Выбор серии → выбор озвучки"""
    parts = callback.data.split(":")
    if len(parts) < 4:
        await callback.answer("Ошибка данных.")
        return

    season_str = parts[1]
    episode_str = parts[2]
    url = ":".join(parts[3:])

    try:
        season = int(season_str)
        episode = int(episode_str)
        item = rezka.get(url)
        translators = item.get_translators(season, episode) if hasattr(item, 'get_translators') else [{"id": "default", "name": "Основная"}]

        kb = InlineKeyboardMarkup(inline_keyboard=[])
        for trans in translators:
            trans_id = trans.get("id", "default")
            trans_name = trans.get("name", "Озвучка")
            kb.inline_keyboard.append([
                InlineKeyboardButton(text=trans_name, callback_data=f"trans:{season}:{episode}:{trans_id}:{url}")
            ])

        await callback.message.edit_text(f"Серия {episode} (сезон {season}): выберите озвучку", reply_markup=kb)
        await callback.answer()

    except Exception as e:
        log.exception("Ошибка обработки серии")
        await callback.answer("Ошибка. Попробуйте позже.", show_alert=True)


@router.callback_query(F.data.startswith("trans:"))
async def handle_translator(callback: CallbackQuery) -> None:
    """Выбор озвучки → показ качеств"""
    parts = callback.data.split(":")
    if len(parts) < 5:
        await callback.answer("Ошибка данных.")
        return

    season_str = parts[1]
    episode_str = parts[2]
    trans_id = parts[3]
    url = ":".join(parts[4:])

    try:
        season = int(season_str)
        episode = int(episode_str)
        item = rezka.get(url)
        streams = item.get_videos(season=season, episode=episode, translator=trans_id) if hasattr(item, 'get_videos') else item.videos

        kb = InlineKeyboardMarkup(inline_keyboard=[])
        for quality, link in streams.items():
            kb.inline_keyboard.append([
                InlineKeyboardButton(text=quality, url=link)
            ])

        await callback.message.edit_text("Выберите качество:", reply_markup=kb)
        await callback.answer()

    except Exception as e:
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

    bot = Bot(settings.player_bot_token or settings.bot_token)
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)
    dp.include_router(router)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
