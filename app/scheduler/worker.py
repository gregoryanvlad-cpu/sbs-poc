from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select

from app.core.config import settings
from app.db.locks import advisory_unlock, try_advisory_lock
from app.db.session import session_scope
from app.repo import list_expired_subscriptions, set_subscription_expired
from app.services.yandex.service import yandex_service
from app.services.yandex.guard import YandexGuardService
from app.db.models.yandex_account import YandexAccount
from app.db.models.yandex_membership import YandexMembership

log = logging.getLogger(__name__)

_guard = YandexGuardService()


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
                        # 1) синкаем семью и активируем тех, кто вошёл правильно (как было)
                        await _job_yandex_sync_and_activate(bot)

                        # 2) guard: кикаем чужих / страйки / баны
                        await _job_yandex_guard(bot)

                        # 3) TTL приглашений (как было)
                        await _job_yandex_invite_ttl(bot)

                        # 4) твой существующий enforcement (как было)
                        await _job_yandex_enforce_no_foreign(bot)
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


async def _job_yandex_enforce_no_foreign(bot: Bot) -> None:
    async with session_scope() as session:
        warnings, _ = await yandex_service.enforce_no_foreign_logins(session)
        if not warnings:
            return
        await session.commit()

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🟡 Yandex Plus", callback_data="nav:yandex")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
        ]
    )
    for tg_id, text in warnings:
        try:
            await bot.send_message(tg_id, text, reply_markup=kb)
        except Exception:
            pass


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
                "Откройте раздел 🟡 Yandex Plus — если доступно, вы сможете запросить новое приглашение (1 раз).",
                reply_markup=kb,
            )
        except Exception:
            pass


async def _job_yandex_guard(bot: Bot) -> None:
    """
    Проверяем, что в семью действительно вошли теми логинами, которые мы ждём.
    Если в семью попал кто-то левый — кикаем и выдаём страйк.
    """
    async with session_scope() as session:
        # Берём 1 активный админский аккаунт (как вы договаривались)
        q_acc = (
            select(YandexAccount)
            .where(YandexAccount.status == "active")
            .order_by(YandexAccount.id.asc())
            .limit(1)
        )
        acc = (await session.execute(q_acc)).scalar_one_or_none()
        if not acc:
            return

        storage_state_path = f"{settings.yandex_cookies_dir}/{acc.credentials_ref}"

        # Берём всех пользователей, у кого сейчас ожидается вступление
        q = (
            select(YandexMembership)
            .where(YandexMembership.status.in_(["awaiting_join", "pending"]))
            .order_by(YandexMembership.id.asc())
            .limit(50)
        )
        res = await session.execute(q)
        items = list(res.scalars().all())

    # вызов guard вне транзакции БД (Playwright может быть долгим)
    for ym in items:
        try:
            expected = (ym.yandex_login or "").strip().lstrip("@").lower()
            if not expected:
                continue

            # allowlist = все логины из membership со статусом joined/pending/awaiting_join
            async with session_scope() as s2:
                q2 = select(YandexMembership.yandex_login).where(
                    YandexMembership.status.in_(["joined", "pending", "awaiting_join"])
                )
                r2 = await s2.execute(q2)
                allowed = [x for x in r2.scalars().all() if x]

            await _guard.verify_join_for_user(
                storage_state_path=storage_state_path,
                tg_id=ym.tg_id,
                expected_login=expected,
                allowed_logins=allowed,
            )
        except Exception:
            log.exception("yandex_guard_error tg_id=%s", getattr(ym, "tg_id", None))
