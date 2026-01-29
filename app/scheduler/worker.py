from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.core.config import settings
from app.db.locks import advisory_unlock, try_advisory_lock
from app.db.session import session_scope
from app.repo import list_expired_subscriptions, set_subscription_expired
from app.services.yandex.service import yandex_service

log = logging.getLogger(__name__)


async def run_scheduler() -> None:
    """
    Scheduler jobs loop (single replica).
    Protected by Postgres advisory lock.
    """
    bot = Bot(token=settings.bot_token)
    log.info("scheduler_start")

    sleep_seconds = min(30, settings.yandex_worker_period_seconds or 10)

    while True:
        try:
            async with session_scope() as session:
                locked = await try_advisory_lock(session)
                if not locked:
                    await asyncio.sleep(3)
                    continue
                try:
                    await _job_expire_subscriptions(bot)
                    if settings.yandex_enabled:
                        await _job_yandex_invite_ttl(bot)
                finally:
                    await advisory_unlock(session)
        except Exception:
            log.exception("scheduler_loop_error")

        await asyncio.sleep(sleep_seconds)


async def _job_expire_subscriptions(bot: Bot) -> None:
    async with session_scope() as session:
        from app.repo import utcnow, deactivate_peers

        now = utcnow()
        expired = await list_expired_subscriptions(session, now)
        if not expired:
            return

        for sub in expired:
            tg_id = sub.tg_id
            await set_subscription_expired(session, tg_id)
            await deactivate_peers(session, tg_id, reason="subscription_expired")
            try:
                await bot.send_message(tg_id, "⛔️ Подписка истекла. Доступ к VPN отключён.")
            except Exception:
                pass

        await session.commit()


async def _job_yandex_invite_ttl(bot: Bot) -> None:
    async with session_scope() as session:
        affected = await yandex_service.expire_pending_invites(session)
        if not affected:
            return
        await session.commit()

    # Уведомления пользователям (после коммита)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🟡 Yandex Plus", callback_data="nav:yandex")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
        ]
    )
    for tg_id in affected:
        try:
            await bot.send_message(
                tg_id,
                "⏳ Время действия приглашения истекло.\n\n"
                "Откройте раздел 🟡 Yandex Plus — если доступно, вы сможете запросить новое приглашение (1 раз).",
                reply_markup=kb,
            )
        except Exception:
            pass
