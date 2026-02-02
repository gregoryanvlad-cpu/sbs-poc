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
    """Scheduler jobs loop (single replica).

    - Subscription expiry: disable VPN and notify user.
    - Manual Yandex rotation: when frozen coverage ended but subscription is still active,
      issue a new invite link from the preloaded pool and notify user.

    Protected by Postgres advisory lock.
    """
    bot = Bot(token=settings.bot_token)
    log.info("scheduler_start")

    sleep_seconds = 30

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
                        await _job_rotate_yandex_invites(bot)
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

            # Manual Yandex process: owner will remove user from the family.
            try:
                await bot.send_message(
                    tg_id,
                    "⛔️ Подписка истекла.\n"
                    "• Доступ к VPN отключён.\n"
                    "• Вы будете исключены из семейной подписки Yandex Plus, так как срок подписки истёк.",
                )
            except Exception:
                pass

        await session.commit()


async def _job_rotate_yandex_invites(bot: Bot) -> None:
    """When user's frozen coverage ended but subscription is still active:
    - issue new invite link
    - notify user
    """
    async with session_scope() as session:
        items = await yandex_service.rotate_due_memberships(session)
        if not items:
            return
        await session.commit()

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🟡 Yandex Plus", callback_data="nav:yandex")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
        ]
    )
    for tg_id, invite_link in items:
        try:
            await bot.send_message(
                tg_id,
                "🔁 Пора перейти в новую семейную подписку Yandex Plus.\n\n"
                "Откройте 🟡 Yandex Plus и нажмите «Открыть приглашение», или используйте ссылку ниже:",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="🔗 Открыть приглашение", url=invite_link)],
                        [InlineKeyboardButton(text="🟡 Yandex Plus", callback_data="nav:yandex")],
                        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
                    ]
                ),
            )
        except Exception:
            # don't break loop
            try:
                await bot.send_message(tg_id, "🔁 Пора перейти в новую семейную подписку Yandex Plus.", reply_markup=kb)
            except Exception:
                pass
