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
from app.services.yandex.guard import YandexGuardService
from app.db.models.yandex_membership import YandexMembership

log = logging.getLogger(__name__)

guard = YandexGuardService()


async def run_scheduler() -> None:
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
                        await _job_yandex_enforce_no_foreign(bot)
                        await _job_yandex_sync_and_activate(bot)
                        await _job_yandex_invite_ttl(bot)
                finally:
                    await advisory_unlock(session)
        except Exception:
            log.exception("scheduler_loop_error")

        await asyncio.sleep(sleep_seconds)


# ========================
# SUBSCRIPTION EXPIRE
# ========================

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


# ========================
# 🔒 GUARD: KICK FOREIGN
# ========================

async def _job_yandex_enforce_no_foreign(bot: Bot) -> None:
    """
    Проверяет, что в семье нет левых логинов.
    Кикает, предупреждает, банит.
    """
    async with session_scope() as session:
        q = (
            session.query(YandexMembership)
            .filter(YandexMembership.status.in_(("awaiting_join", "joined")))
            .all()
        )

    for ym in q:
        if not ym.yandex_login or not ym.storage_state_path:
            continue
        if ym.status == "banned":
            continue

        try:
            await guard.verify_join(
                yandex_account_storage=ym.storage_state_path,
                expected_login=ym.yandex_login.lower(),
                tg_id=ym.tg_id,
            )
        except Exception:
            log.exception("guard_verify_failed tg_id=%s", ym.tg_id)


# ========================
# SYNC + ACTIVATE
# ========================

async def _job_yandex_sync_and_activate(bot: Bot) -> None:
    async with session_scope() as session:
        activated, _ = await yandex_service.sync_family_and_activate(session)
        if not activated:
            return
        await session.commit()

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🟡 Yandex Plus", callback_data="nav:yandex")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
        ]
    )
    for tg_id in activated:
        try:
            await bot.send_message(
                tg_id,
                "✅ Вы успешно подключены к семейной подписке Yandex Plus.\n\n"
                "Откройте раздел 🟡 Yandex Plus — там будет ваш статус.",
                reply_markup=kb,
            )
        except Exception:
            pass


# ========================
# INVITE TTL
# ========================

async def _job_yandex_invite_ttl(bot: Bot) -> None:
    async with session_scope() as session:
        affected = await yandex_service.expire_pending_invites(session)
        if not affected:
            return
        await session.commit()

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
                "Откройте раздел 🟡 Yandex Plus — если доступно, вы сможете запросить новое приглашение.",
                reply_markup=kb,
            )
        except Exception:
            pass
