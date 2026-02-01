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
from app.services.vpn.guard import YandexGuardService
from app.db.models.yandex_account import YandexAccount
from app.db.models.yandex_membership import YandexMembership

log = logging.getLogger(__name__)

_guard = YandexGuardService()


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
                        # Yandex jobs are event-driven:
                        # - Heavy probing (family scans) only while there is an active invite (awaiting_join with link)
                        # - TTL expiry / missing-invite issuance runs when needed
                        active_invite = await _has_active_yandex_invites()
                        expired_invite = await _has_expired_yandex_invites()
                        needs_invite = await _has_pending_invites_without_link() or await _has_reactivated_removed()

                        # 1) TTL: if any invite expired, free the slot
                        if expired_invite:
                            await _job_yandex_invite_ttl(bot)

                        # 2) Issue missing invites (created but link not ready) and reinvite reactivated users
                        if needs_invite:
                            await _job_yandex_issue_needed_invites(bot)

                        # 3) Only during active invite window we do family scanning & guards.
                        if active_invite:
                            await _job_yandex_sync_and_activate(bot)
                            await _job_yandex_guard(bot)
                            await _job_yandex_enforce_no_foreign(bot)
                finally:
                    await advisory_unlock(session)
        except Exception:
            log.exception("scheduler_loop_error")

        await asyncio.sleep(sleep_seconds)


async def _has_active_yandex_invites() -> bool:
    """Active invite = awaiting_join with non-empty invite_link and not expired."""
    from app.repo import utcnow

    now = utcnow()
    async with session_scope() as session:
        q = select(YandexMembership.id).where(
            YandexMembership.status == "awaiting_join",
            YandexMembership.invite_link.is_not(None),
            YandexMembership.invite_expires_at.is_not(None),
            YandexMembership.invite_expires_at > now,
        ).limit(1)
        return (await session.scalar(q)) is not None


async def _has_expired_yandex_invites() -> bool:
    from app.repo import utcnow

    now = utcnow()
    async with session_scope() as session:
        q = select(YandexMembership.id).where(
            YandexMembership.status == "awaiting_join",
            YandexMembership.invite_expires_at.is_not(None),
            YandexMembership.invite_expires_at <= now,
        ).limit(1)
        return (await session.scalar(q)) is not None


async def _has_pending_invites_without_link() -> bool:
    async with session_scope() as session:
        q = select(YandexMembership.id).where(
            YandexMembership.status == "pending",
            YandexMembership.invite_link.is_(None),
        ).limit(1)
        return (await session.scalar(q)) is not None


async def _has_reactivated_removed() -> bool:
    """True if there are removed users with active subscription (needs re-invite)."""
    from app.db.models.subscription import Subscription
    from app.repo import utcnow

    now = utcnow()
    async with session_scope() as session:
        q = (
            select(YandexMembership.id)
            .join(Subscription, Subscription.tg_id == YandexMembership.tg_id)
            .where(
                YandexMembership.status == "removed",
                Subscription.end_at.is_not(None),
                Subscription.end_at > now,
            )
            .limit(1)
        )
        return (await session.scalar(q)) is not None


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

            # Also remove from Yandex family (best-effort).
            try:
                await yandex_service.remove_user_from_family_if_needed(session=session, tg_id=tg_id)
            except Exception:
                pass

            # User notification (single message, no extra noise).
            # We treat Yandex removal as a part of access revocation even if the external action
            # may be delayed due to temporary errors (captcha/network) — the user access in our
            # system is already stopped.
            try:
                await bot.send_message(
                    tg_id,
                    "⛔️ Подписка истекла.\n"
                    "• Доступ к VPN отключён.\n"
                    "• Вы исключены из семейной подписки Yandex Plus, так как срок подписки истёк.",
                )
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


async def _job_yandex_issue_needed_invites(bot: Bot) -> None:
    """Issue invites for:
    - pending memberships with no invite_link yet (created earlier)
    - removed users who have an active subscription again

    We keep user UX simple: user just receives the invite when ready.
    """
    async with session_scope() as session:
        issued = []
        try:
            issued += await yandex_service.issue_missing_invites(session)
        except Exception:
            pass
        try:
            issued += await yandex_service.issue_invites_for_reactivated_users(session)
        except Exception:
            pass

        if not issued:
            return

        await session.commit()

    for m in issued:
        if not getattr(m, "invite_link", None):
            continue
        try:
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🔗 Открыть приглашение", url=m.invite_link)],
                    [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
                ]
            )
            await bot.send_message(
                m.tg_id,
                "✅ Логин принят.\n\n"
                f"Логин: <code>{m.yandex_login}</code>\n"
                "Статус: ⏳ <b>Ожидание вступления</b>\n\n"
                "Нажми кнопку ниже и прими приглашение:",
                reply_markup=kb,
                parse_mode="HTML",
            )
        except Exception:
            pass


async def _job_yandex_guard(bot: Bot) -> None:
    """
    Guard по expected-логину:
    если ожидаемый логин НЕ в гостях, но есть другие гости — кикаем их и выдаём страйк ожидающему.
    """
    # 1) Берём активный YandexAccount
    async with session_scope() as session:
        q_acc = (
            select(YandexAccount)
            .where(YandexAccount.status == "active")
            .order_by(YandexAccount.id.asc())
            .limit(1)
        )
        acc = (await session.execute(q_acc)).scalar_one_or_none()
        if not acc or not acc.credentials_ref:
            return

        storage_state_path = f"{settings.yandex_cookies_dir}/{acc.credentials_ref}"

        # 2) Берём memberships, которые ждут вступление
        q = (
            select(YandexMembership)
            .where(YandexMembership.status.in_(["awaiting_join", "pending"]))
            .order_by(YandexMembership.id.asc())
            .limit(50)
        )
        res = await session.execute(q)
        items = list(res.scalars().all())

    # 3) Вызов guard (Playwright) — вне транзакции БД
    for ym in items:
        try:
            expected = (ym.yandex_login or "").strip().lstrip("@").lower()
            if not expected:
                continue

            # ✅ ВАЖНО: используем именно verify_join (он у тебя точно есть)
            await _guard.verify_join(
                yandex_account_storage=storage_state_path,
                expected_login=expected,
                tg_id=ym.tg_id,
            )

        except Exception:
            log.exception("yandex_guard_error tg_id=%s", getattr(ym, "tg_id", None))
