from __future__ import annotations

import asyncio
import logging

from aiogram import Bot

from app.core.config import settings
from app.db.locks import advisory_unlock, try_advisory_lock
from app.db.session import session_scope
from app.repo import list_expired_subscriptions, set_subscription_expired
from app.services.vpn.service import vpn_service

log = logging.getLogger(__name__)


async def run_scheduler() -> None:
    """Runs scheduler jobs loop.

    Default period: 30 seconds.
    Protected by Postgres advisory lock.
    """
    bot = Bot(token=settings.bot_token)
    log.info("scheduler_start")
    while True:
        try:
            async with session_scope() as session:
                locked = await try_advisory_lock(session)
                if not locked:
                    await asyncio.sleep(3)
                    continue
                try:
                    await _job_expire_subscriptions(bot)
                finally:
                    await advisory_unlock(session)
        except Exception:
            log.exception("scheduler_loop_error")

        await asyncio.sleep(30)


async def _job_expire_subscriptions(bot: Bot) -> None:
    async with session_scope() as session:
        from app.repo import utcnow

        now = utcnow()
        expired = await list_expired_subscriptions(session, now)
        if not expired:
            return

        for sub in expired:
            tg_id = sub.tg_id
            await set_subscription_expired(session, tg_id)
            # deactivate all peers (mock) - real WG revocation will be added later
            from app.repo import deactivate_peers

            await deactivate_peers(session, tg_id, reason="subscription_expired")
            try:
                await bot.send_message(tg_id, "⛔️ Подписка истекла. Доступ к VPN отключён.")
            except Exception:
                pass

        await session.commit()