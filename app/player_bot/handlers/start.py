from __future__ import annotations

import logging
from urllib.parse import urlparse

from aiogram import Router
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.core.config import settings
from app.db.session import session_scope
from app.repo import get_content_request_by_token, get_subscription
from app.bot.ui import utcnow

router = Router()

log = logging.getLogger(__name__)


def _is_sub_active(end_at) -> bool:
    if not end_at:
        return False
    try:
        return end_at > utcnow()
    except Exception:
        return False


def _domain_allowed(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False

    if not host:
        return False

    for d in settings.player_whitelist_domains:
        d = (d or "").lower()
        if not d:
            continue
        # allow exact domain or subdomains
        if host == d or host.endswith("." + d):
            return True
    return False


@router.message()
async def on_start(msg: Message) -> None:
    """Handle /start <token> deep link."""
    if not msg.text or not msg.text.startswith("/start"):
        return

    parts = msg.text.split(maxsplit=1)
    token = parts[1].strip() if len(parts) > 1 else ""

    if not token:
        await msg.answer(
            "Это плеер-бот. Открой контент из основного бота: @" + settings.main_bot_username
        )
        return

    # 1) Check subscription
    async with session_scope() as session:
        sub = await get_subscription(session, msg.from_user.id)
        if not _is_sub_active(sub.end_at):
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Перейти в основной бот",
                            url=f"https://t.me/{settings.main_bot_username}",
                        )
                    ]
                ]
            )
            await msg.answer(
                "⛔️ У вас нет активной подписки.\n\nОформите её в основном боте.",
                reply_markup=kb,
            )
            return

        # 2) Resolve token
        req = await get_content_request_by_token(session, token)
        if not req:
            await msg.answer("⚠️ Ссылка устарела. Открой контент заново из основного бота.")
            return

        if req.user_id != msg.from_user.id:
            await msg.answer("⚠️ Эта ссылка предназначена для другого пользователя.")
            return

        content_url = req.content_url

    # 3) Domain allowlist check
    if not _domain_allowed(content_url):
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Вернуться в основной бот", url=f"https://t.me/{settings.main_bot_username}")]
            ]
        )
        await msg.answer(
            "⚠️ Источник не поддерживается для просмотра внутри Telegram.\n\n"
            "Открой контент из разрешённых источников.",
            reply_markup=kb,
        )
        return

    # 4) We only отдаём прямую ссылку (m3u8/mp4) — без скачивания.
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="▶️ Смотреть", url=content_url)]]
    )
    await msg.answer("Готово ✅\nНажми кнопку ниже:", reply_markup=kb)
