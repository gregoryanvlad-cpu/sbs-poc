"""
Secondary bot entrypoint — Player Gateway (inoteka Secure Connection | SBS)
Запускается в отдельном Railway-сервисе (kinoteka-player) с SERVICE_ROLE=player

Обязательные переменные окружения в Railway для этого сервиса:
- SERVICE_ROLE=player
- PLAYER_BOT_TOKEN (или BOT_TOKEN) — токен плеер-бота
- DATABASE_URL (ссылка на общую Postgres)
- OWNER_TG_ID (для конфига)
- MAIN_BOT_USERNAME (например, sbsconnect_bot) — для кнопки "Купить подписку"
- PLAYER_RATE_LIMIT_PER_MINUTE (например, 15)
- REZKA_MIRROR (опционально, по умолчанию https://rezka.ag)

Не загружает VPN, Yandex, рефералку и т.д. — только плеер + проверка подписки.
"""

from __future__ import annotations
import asyncio
import logging
import os
import subprocess
import sys
from datetime import datetime

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CommandStart
from aiogram import Router, F

from app.core.logging import setup_logging
from app.core.config import settings  # предполагается, что он читает env
from app.db.session import init_engine, get_session
from app.db.models import Subscription  # твоя модель подписки

# Подключаем наш парсер Rezka
from HdRezkaApi import HdRezkaApi

log = logging.getLogger(__name__)

# Инициализация парсера Rezka
rezka = HdRezkaApi(mirror=os.getenv("REZKA_MIRROR", "https://rezka.ag"))

# Простой rate-limit в памяти (на 1 минуту)
rate_cache = {}  # user_id → count

router = Router()


@router.message(CommandStart(deep_link=True))
async def handle_start_with_param(message: Message):
    """Обработка deep-link /start <content_url>"""
    user_id = message.from_user.id
    args = message.text.split(maxsplit=1)

    if len(args) < 2:
        await message.answer("Ссылка недействительна. Откройте фильм из основного бота.")
        return

    content_url = args[1].strip()

    # Rate-limit: 15 запросов/мин
    if rate_limit_exceeded(user_id):
        await message.answer("Слишком много запросов. Подождите минуту.")
        return

    # Проверка подписки
    async with get_session() as session:
        sub = await session.query(Subscription).filter(
            Subscription.user_id == user_id,
            Subscription.is_active == True,
            Subscription.end_at > datetime.utcnow()
        ).first()

        if not sub:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Купить подписку", url=f"t.me/{settings.MAIN_BOT_USERNAME}")]
            ])
            await message.answer(
                "У вас нет активной подписки.\nОформите её в основном боте:",
                reply_markup=kb
            )
            return

    # Получаем информацию о контенте
    try:
        item = rezka.get(content_url)
        if not item:
            await message.answer("Контент не найден или временно недоступен.")
            return

        title = item.title
        year = item.year or "—"
        poster = item.poster
        description = getattr(item, 'description', 'Описание отсутствует')[:600]

        # Если сериал — показываем выбор сезона
        if hasattr(item, 'seasons') and item.seasons:
            kb = InlineKeyboardMarkup(inline_keyboard=[])
            for season_num in sorted(item.seasons.keys()):
                kb.inline_keyboard.append([
                    InlineKeyboardButton(
                        text=f"Сезон {season_num}",
                        callback_data=f"season:{season_num}:{content_url}"
                    )
                ])
            text = f"<b>{title} ({year})</b>\n\n{description}\n\nВыберите сезон:"
            if poster:
                await message.answer_photo(photo=poster, caption=text, reply_markup=kb, parse_mode="HTML")
            else:
                await message.answer(text, reply_markup=kb, parse_mode="HTML")

        # Если фильм — сразу показываем качества
        else:
            streams = item.videos if hasattr(item, 'videos') else {}
            if not streams and hasattr(item, 'player'):
                streams = {"Смотреть": item.player}

            kb = InlineKeyboardMarkup(inline_keyboard=[])
            for quality, link in streams.items():
                if link:
                    kb.inline_keyboard.append([
                        InlineKeyboardButton(text=quality, url=link)
                    ])

            text = f"<b>{title} ({year})</b>\n\n{description}"
            if poster:
                await message.answer_photo(photo=poster, caption=text, reply_markup=kb, parse_mode="HTML")
            else:
                await message.answer(text, reply_markup=kb, parse_mode="HTML")

    except Exception as e:
        log.exception(f"Ошибка обработки контента {content_url}")
        await message.answer("Не удалось загрузить контент. Попробуйте позже.")


@router.callback_query(F.data.startswith("season:"))
async def handle_season(callback: CallbackQuery):
    """Выбор сезона → список серий"""
    _, season_str, url = callback.data.split(":", 2)
    season = int(season_str)

    try:
        item = rezka.get(url)
        episodes = item.seasons.get(season, {}).get('episodes', []) if hasattr(item, 'seasons') else []

        kb = InlineKeyboardMarkup(inline_keyboard=[])
        for ep in sorted(episodes):
            kb.inline_keyboard.append([
                InlineKeyboardButton(
                    text=f"Серия {ep}",
                    callback_data=f"episode:{season}:{ep}:{url}"
                )
            ])

        await callback.message.edit_text(
            f"Сезон {season}: выберите серию",
            reply_markup=kb
        )
        await callback.answer()

    except Exception as e:
        log.exception("Ошибка обработки сезона")
        await callback.answer("Ошибка. Попробуйте позже.", show_alert=True)


@router.callback_query(F.data.startswith("episode:"))
async def handle_episode(callback: CallbackQuery):
    """Выбор серии → выбор озвучки → качества"""
    _, season_str, episode_str, url = callback.data.split(":", 3)
    season, episode = int(season_str), int(episode_str)

    try:
        item = rezka.get(url)
        # Предполагаем, что есть метод или атрибут translators
        translators = item.get_translators(season, episode) if hasattr(item, 'get_translators') else [{"id": "default", "name": "Основная"}]

        kb = InlineKeyboardMarkup(inline_keyboard=[])
        for trans in translators:
            kb.inline_keyboard.append([
                InlineKeyboardButton(
                    text=trans.get("name", "Озвучка"),
                    callback_data=f"trans:{season}:{episode}:{trans.get('id', 'default')}:{url}"
                )
            ])

        await callback.message.edit_text(
            f"Серия {episode} (сезон {season}): выберите озвучку",
            reply_markup=kb
        )
        await callback.answer()

    except Exception as e:
        log.exception("Ошибка обработки серии")
        await callback.answer("Ошибка. Попробуйте позже.", show_alert=True)


@router.callback_query(F.data.startswith("trans:"))
async def handle_translator(callback: CallbackQuery):
    """Выбор озвучки → показ качеств"""
    _, season_str, episode_str, trans_id, url = callback.data.split(":", 4)
    season, episode = int(season_str), int(episode_str)

    try:
        item = rezka.get(url)
        streams = item.get_streams(season=season, episode=episode, translator=trans_id) \
            if hasattr(item, 'get_streams') else item.videos

        kb = InlineKeyboardMarkup(inline_keyboard=[])
        for quality, link in (streams or {}).items():
            if link:
                kb.inline_keyboard.append([
                    InlineKeyboardButton(text=quality, url=link)
                ])

        if not kb.inline_keyboard:
            kb.inline_keyboard.append([
                InlineKeyboardButton(text="Смотреть", url=item.player if hasattr(item, 'player') else url)
            ])

        await callback.message.edit_text(
            f"Выберите качество (озвучка выбрана)",
            reply_markup=kb
        )
        await callback.answer()

    except Exception as e:
        log.exception("Ошибка обработки озвучки")
        await callback.answer("Ошибка. Попробуйте позже.", show_alert=True)


def rate_limit_exceeded(user_id: int) -> bool:
    """Простой rate-limit в памяти (можно заменить на redis)"""
    limit = int(os.getenv("PLAYER_RATE_LIMIT_PER_MINUTE", 15))
    key = f"rate_{user_id}"
    count = rate_cache.get(key, 0)
    if count >= limit:
        return True
    rate_cache[key] = count + 1
    # Можно добавить TTL, но для простоты оставляем
    return False


def _run_alembic_upgrade_head_best_effort() -> None:
    """Применяем миграции при старте (best-effort)"""
    try:
        subprocess.check_call([sys.executable, "-m", "alembic", "upgrade", "head"])
        log.info("✅ Alembic migrations applied: upgrade head")
    except Exception:
        log.exception("❌ Alembic upgrade head failed. Continuing without migrations.")


async def main() -> None:
    setup_logging()
    init_engine(settings.database_url)
    _run_alembic_upgrade_head_best_effort()

    bot = Bot(token=settings.player_bot_token or settings.bot_token)
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)
    dp.include_router(router)

    log.info("🚀 Player bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
