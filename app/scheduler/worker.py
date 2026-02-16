from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone, date
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select, text, func, delete

from app.core.config import settings
from app.db.locks import advisory_unlock, try_advisory_lock
from app.db.session import session_scope
from app.db.models import Payment, Subscription, VpnPeer
from app.db.models.region_vpn_session import RegionVpnSession
from app.db.models.yandex_membership import YandexMembership
from app.repo import list_expired_subscriptions, set_subscription_expired
from app.services.yandex.service import yandex_service
from app.services.referrals.service import referral_service
from app.services.regionvpn.service import RegionVpnService

log = logging.getLogger(__name__)

AMSTERDAM_TZ = ZoneInfo("Europe/Amsterdam")

# how often scheduler loops
SLEEP_SECONDS = 30

# internal state keys (stored in DB best-effort)
JOBSTATE_DAILY_KICK_REPORT_LAST_DATE = "daily_kick_report_last_date"  # YYYY-MM-DD


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_tz(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _days_until(dt: datetime, now: datetime) -> int:
    """Ceil-like day boundary logic but stable for notifications.

    We treat 'in 1 day' if remaining <= 1 day and > 0.
    """
    dt = _ensure_tz(dt)
    now = _ensure_tz(now)
    delta = dt - now
    # negative -> already passed
    if delta.total_seconds() <= 0:
        return 0
    return int((delta.total_seconds() + 86399) // 86400)


async def _jobstate_get(session, key: str) -> str | None:
    """Best-effort read from job_state table. If table doesn't exist yet, return None."""
    try:
        res = await session.execute(text("SELECT value FROM job_state WHERE key = :k"), {"k": key})
        return res.scalar_one_or_none()
    except Exception:
        return None


async def _jobstate_set(session, key: str, value: str) -> None:
    """Best-effort upsert into job_state table. If table doesn't exist yet, do nothing."""
    try:
        await session.execute(
            text(
                "INSERT INTO job_state(key, value, updated_at) "
                "VALUES (:k, :v, now()) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"
            ),
            {"k": key, "v": value},
        )
    except Exception:
        # ignore if table not yet migrated
        return


def _is_extended(sub_end_at: datetime | None, coverage_end_at: datetime | None) -> bool:
    """User is considered 'extended' if subscription end is AFTER frozen coverage end."""
    if not sub_end_at or not coverage_end_at:
        return False
    return _ensure_tz(sub_end_at) > _ensure_tz(coverage_end_at)


def _fmt_dt(dt: datetime | None) -> str:
    if not dt:
        return "—"
    dt = _ensure_tz(dt)
    return dt.astimezone(AMSTERDAM_TZ).strftime("%d.%m.%Y %H:%M")


def _human_age(from_dt: datetime | None, now: datetime) -> str:
    if not from_dt:
        return "—"
    from_dt = _ensure_tz(from_dt)
    delta = now - from_dt
    days = max(0, int(delta.total_seconds() // 86400))
    years = days // 365
    months = (days % 365) // 30
    rem = days - years * 365 - months * 30
    parts = []
    if years:
        parts.append(f"{years} г.")
    if months:
        parts.append(f"{months} мес.")
    parts.append(f"{rem} дн.")
    return " ".join(parts)


async def build_kick_report_text(session) -> str:
    """Builds admin report for users whose subscription ended and who were not marked removed."""
    now = _utcnow()

    # we consider due-to-kick if subscription already expired (end_at <= now)
    # and membership exists with account_label & slot_index (so you can kick by slot)
    q = (
        select(YandexMembership, Subscription)
        .join(Subscription, Subscription.tg_id == YandexMembership.tg_id)
        .where(
            Subscription.end_at.is_not(None),
            Subscription.end_at <= now,
            YandexMembership.removed_at.is_(None),
        )
        .order_by(Subscription.end_at.asc(), YandexMembership.id.asc())
        .limit(200)
    )
    rows = (await session.execute(q)).all()

    if not rows:
        return "Сегодня участников для исключения нет ✅"

    lines: list[str] = []
    lines.append("Сегодня пора исключить следующих участников из следующих семей:\n")

    idx = 1
    for m, sub in rows:
        tg_id = int(m.tg_id)

        # last successful payment date (best-effort)
        pay_q = (
            select(func.max(Payment.paid_at))
            .where(Payment.tg_id == tg_id, Payment.status == "success")
        )
        paid_at = (await session.execute(pay_q)).scalar_one_or_none()

        # real VPN status
        peer_q = (
            select(VpnPeer)
            .where(VpnPeer.tg_id == tg_id, VpnPeer.is_active == True)
            .order_by(VpnPeer.id.desc())
            .limit(1)
        )
        peer = (await session.execute(peer_q)).scalar_one_or_none()
        vpn_status = "Включен" if peer else "Отключен"

        # renewed?
        renewed = False
        try:
            renewed = _is_extended(sub.end_at, m.coverage_end_at)
        except Exception:
            renewed = False

        # for this report we mainly show those not renewed; but keep field truthful
        renewed_text = "Продлевалась" if renewed else "Не продлевалась"

        lines.append(f"#{idx}")
        lines.append(f"Пользователь ID TG: {tg_id}")
        lines.append(f"Дата приобретения подписки на сервис: {_fmt_dt(paid_at) if paid_at else _fmt_dt(sub.start_at)}")
        lines.append(f"Дата окончания подписки на сервис: {_fmt_dt(sub.end_at)}")
        lines.append(f"Наименование семьи (label): {m.account_label or '—'}")
        lines.append(f"Номер слота: {m.slot_index if m.slot_index is not None else '—'}")
        lines.append(f"VPN: {vpn_status}")
        lines.append(f"Подписка: {renewed_text}")
        lines.append(f"Пользователь с нами: {_human_age(sub.start_at, now)}")
        lines.append("")  # blank line

        idx += 1

    return "\n".join(lines).strip()


async def _send_admin_kick_report(bot: Bot, *, force: bool = False) -> None:
    """Send admin kick report daily at 12:00 Amsterdam, and also callable manually (force=True)."""
    owner_id = int(settings.owner_tg_id)
    now_local = datetime.now(AMSTERDAM_TZ)
    today_str = now_local.date().isoformat()

    async with session_scope() as session:
        # if not force -> only send exactly once per day, around 12:00
        if not force:
            if not (now_local.hour == 12 and now_local.minute == 0):
                return

            last = await _jobstate_get(session, JOBSTATE_DAILY_KICK_REPORT_LAST_DATE)
            if last == today_str:
                return

        text_report = await build_kick_report_text(session)

        # mark as sent (best-effort)
        if not force:
            await _jobstate_set(session, JOBSTATE_DAILY_KICK_REPORT_LAST_DATE, today_str)
            await session.commit()

    try:
        await bot.send_message(owner_id, text_report)
    except Exception:
        pass


async def run_scheduler() -> None:
    """Scheduler jobs loop (single replica) protected by advisory lock.

    Jobs:
    - Expire subscriptions (VPN auto-disable + notify).
    - Rotate Yandex invites when coverage ended but subscription is active.
    - Send user reminders 7/3/1 for non-renewed users.
    - Send user 1-day 'new invite tomorrow' notice for renewed users.
    - Daily admin kick report at 12:00 Amsterdam (+ manual trigger will call same builder).
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
                    await _job_prune_regionvpn_clients()
                    if settings.yandex_enabled:
                        await _job_rotate_yandex_invites(bot)
                    await _job_user_subscription_notifications(bot)
                    # Make pending referral earnings available when hold expires.
                    try:
                        released = await referral_service.release_pending(session)
                        if released:
                            await session.commit()
                    except Exception:
                        pass
                    await _send_admin_kick_report(bot, force=False)
                finally:
                    await advisory_unlock(session)
        except Exception:
            log.exception("scheduler_loop_error")

        await asyncio.sleep(SLEEP_SECONDS)


async def _job_expire_subscriptions(bot: Bot) -> None:
    async with session_scope() as session:
        from app.repo import utcnow, deactivate_peers

        now = utcnow()
        expired = await list_expired_subscriptions(session, now)
        if not expired:
            return

        expired_ids: list[int] = []
        for sub in expired:
            tg_id = sub.tg_id
            await set_subscription_expired(session, tg_id)
            await deactivate_peers(session, tg_id, reason="subscription_expired")
            expired_ids.append(tg_id)

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

    # Disable RegionVPN clients (keep UUIDs, just block traffic) so user can renew
    # within 24h and keep the same config.
    if settings.regionvpn_enabled and expired_ids:
        svc = RegionVpnService(
            ssh_host=settings.regionvpn_ssh_host,
            ssh_user=settings.regionvpn_ssh_user,
            ssh_key_path=settings.regionvpn_ssh_key_path,
            xray_config_path=settings.regionvpn_xray_config_path,
            xray_restart_command=settings.regionvpn_xray_restart_command,
            vless_tag=settings.regionvpn_vless_tag,
            vless_port=settings.regionvpn_vless_port,
            vless_sni=settings.regionvpn_vless_sni,
            vless_pbk=settings.regionvpn_vless_pbk,
            vless_sid=settings.regionvpn_vless_sid,
            vless_flow=settings.regionvpn_vless_flow,
        )
        await svc.apply_enabled_map({tg_id: False for tg_id in expired_ids})


async def _job_prune_regionvpn_clients() -> None:
    """After 24 hours of inactivity (expired subscription), remove the client from Xray.

    This keeps the same config usable for 24h after expiration (while blocked),
    but eventually frees up the server state.
    """

    # Backwards compatible safety: older configs may not have the flag.
    if not getattr(settings, "regionvpn_enabled", True):
        return

    cutoff = _utcnow() - timedelta(days=1)

    async with session_scope() as session:
        rows = await session.execute(
            select(Subscription.tg_id)
            .where(Subscription.is_active.is_(False))
            .where(Subscription.end_at < cutoff)
        )
        tg_ids = list(rows.scalars().all())
        if not tg_ids:
            return

        svc = RegionVpnService(
            ssh_host=settings.regionvpn_ssh_host,
            ssh_user=settings.regionvpn_ssh_user,
            ssh_key_path=settings.regionvpn_ssh_key_path,
            xray_config_path=settings.regionvpn_xray_config_path,
            xray_restart_command=settings.regionvpn_xray_restart_command,
            vless_tag=settings.regionvpn_vless_tag,
            vless_port=settings.regionvpn_vless_port,
            vless_sni=settings.regionvpn_vless_sni,
            vless_pbk=settings.regionvpn_vless_pbk,
            vless_sid=settings.regionvpn_vless_sid,
            vless_flow=settings.regionvpn_vless_flow,
        )

        # Revoke from server config (idempotent). Also drop local session tracking.
        for tg_id in tg_ids:
            try:
                await svc.revoke_client(tg_id)
            except Exception:
                pass

            await session.execute(delete(RegionVpnSession).where(RegionVpnSession.tg_id == tg_id))

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
                await bot.send_message(
                    tg_id,
                    "🔁 Пора перейти в новую семейную подписку Yandex Plus.",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(text="🟡 Yandex Plus", callback_data="nav:yandex")],
                            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
                        ]
                    ),
                )
            except Exception:
                pass


async def _job_user_subscription_notifications(bot: Bot) -> None:
    """User notifications based on coverage_end_at and extension state.

    Rules:
    - If NOT renewed: send 7/3/1 days before subscription end (coverage_end_at).
    - If renewed: do NOT send 7/3, only 1-day notice about new invite tomorrow.
    - Each notification is sent once using notified_* fields.
    """
    now = _utcnow()

    async with session_scope() as session:
        q = (
            select(YandexMembership, Subscription)
            .join(Subscription, Subscription.tg_id == YandexMembership.tg_id)
            .where(
                YandexMembership.coverage_end_at.is_not(None),
                Subscription.end_at.is_not(None),
                Subscription.end_at > now,  # only active users
                YandexMembership.removed_at.is_(None),
            )
            .order_by(YandexMembership.id.asc())
            .limit(500)
        )
        rows = (await session.execute(q)).all()
        if not rows:
            return

        # we will update flags and commit once
        changed = False

        for m, sub in rows:
            cov = _ensure_tz(m.coverage_end_at)
            sub_end = _ensure_tz(sub.end_at)

            days_left = _days_until(cov, now)

            renewed = _is_extended(sub_end, cov)

            # 🟢 Renewed users: only 1-day "tomorrow new invite" notice
            if renewed:
                if days_left == 1 and m.notified_1d_at is None:
                    try:
                        await bot.send_message(
                            int(m.tg_id),
                            "ℹ️ Завтра вам будет выдано новое приглашение в семейную подписку Yandex Plus.\n\n"
                            "Это связано со сменой аккаунта. Никаких действий сейчас не требуется.",
                            reply_markup=InlineKeyboardMarkup(
                                inline_keyboard=[
                                    [InlineKeyboardButton(text="🟡 Yandex Plus", callback_data="nav:yandex")],
                                    [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
                                ]
                            ),
                        )
                        m.notified_1d_at = now
                        changed = True
                    except Exception:
                        pass
                continue

            # 🔴 Not renewed users: 7 / 3 / 1 days expiry warnings
            if days_left == 7 and m.notified_7d_at is None:
                try:
                    await bot.send_message(
                        int(m.tg_id),
                        "⏳ Через 7 дней закончится ваша подписка на сервис.\n\n"
                        "Продлите подписку, чтобы сохранить доступ к VPN и Yandex Plus.",
                        reply_markup=InlineKeyboardMarkup(
                            inline_keyboard=[
                                [InlineKeyboardButton(text="💳 Оплата", callback_data="nav:pay")],
                                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
                            ]
                        ),
                    )
                    m.notified_7d_at = now
                    changed = True
                except Exception:
                    pass

            if days_left == 3 and m.notified_3d_at is None:
                try:
                    await bot.send_message(
                        int(m.tg_id),
                        "⚠️ Осталось 3 дня до окончания подписки.\n\n"
                        "Продлите подписку, чтобы не потерять доступ к VPN и Yandex Plus.",
                        reply_markup=InlineKeyboardMarkup(
                            inline_keyboard=[
                                [InlineKeyboardButton(text="💳 Оплата", callback_data="nav:pay")],
                                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
                            ]
                        ),
                    )
                    m.notified_3d_at = now
                    changed = True
                except Exception:
                    pass

            if days_left == 1 and m.notified_1d_at is None:
                try:
                    await bot.send_message(
                        int(m.tg_id),
                        "🚨 Завтра ваша подписка закончится.\n\n"
                        "• VPN будет отключён.\n"
                        "• Доступ к Yandex Plus завершится.\n\n"
                        "Продлите подписку, чтобы сохранить доступ.",
                        reply_markup=InlineKeyboardMarkup(
                            inline_keyboard=[
                                [InlineKeyboardButton(text="💳 Оплата", callback_data="nav:pay")],
                                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
                            ]
                        ),
                    )
                    m.notified_1d_at = now
                    changed = True
                except Exception:
                    pass

        if changed:
            await session.commit()
