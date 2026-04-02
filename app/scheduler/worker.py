from __future__ import annotations

import asyncio
import json
import os
import logging
from datetime import datetime, timedelta, timezone, date
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select, text, func, delete, or_
from sqlalchemy.orm import aliased

from app.core.config import settings
from app.db.locks import advisory_unlock, try_advisory_lock
from app.db.session import session_scope
from app.db.models import Payment, Subscription, VpnPeer, FamilyVpnGroup, FamilyVpnProfile, Referral, ReferralEarning
from app.db.models import LteVpnClient
from app.db.models.region_vpn_session import RegionVpnSession
from app.db.models.yandex_membership import YandexMembership
from app.repo import list_expired_subscriptions, set_subscription_expired, get_app_setting_int, set_app_setting_int, get_subscription, extend_subscription
from app.services.yandex.service import yandex_service
from app.services.referrals.service import referral_service
from app.services.regionvpn.service import RegionVpnService
from app.services.lte_vpn.service import lte_vpn_service
from app.services.message_audit import audit_send_message

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


def _get_scheduler_vpn_servers() -> list[dict]:
    raw = (os.environ.get("VPN_SERVERS_JSON") or os.environ.get("VPN_SERVERS") or "").strip()
    out: list[dict] = []
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and "servers" in data:
                data = data["servers"]
            if isinstance(data, list):
                out = [x for x in data if isinstance(x, dict)]
        except Exception:
            out = []
    if out:
        return out
    code = (os.environ.get("VPN_CODE") or "NL").upper()
    return [{
        "code": code,
        "name": os.environ.get("VPN_NAME") or code,
        "host": os.environ.get("WG_SSH_HOST"),
        "port": int(os.environ.get("WG_SSH_PORT", "22") or 22),
        "user": os.environ.get("WG_SSH_USER"),
        "password": os.environ.get("WG_SSH_PASSWORD"),
        "interface": os.environ.get("VPN_INTERFACE", "wg0"),
    }]


def _get_scheduler_vpn_servers() -> list[dict]:
    loader = globals().get("_load_vpn_servers_for_scheduler")
    if callable(loader):
        try:
            data = loader()
            if isinstance(data, list):
                return data
        except Exception:
            pass
    raw = (os.environ.get("VPN_SERVERS_JSON") or os.environ.get("VPN_SERVERS") or "").strip()
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and "servers" in data:
                data = data["servers"]
            if isinstance(data, list):
                return [x for x in data if isinstance(x, dict)]
        except Exception:
            pass
    code = (os.environ.get("VPN_CODE") or "NL").upper()
    return [{
        "code": code,
        "name": os.environ.get("VPN_NAME") or code,
        "host": os.environ.get("WG_SSH_HOST"),
        "port": int(os.environ.get("WG_SSH_PORT", "22") or 22),
        "user": os.environ.get("WG_SSH_USER"),
        "password": os.environ.get("WG_SSH_PASSWORD"),
        "interface": os.environ.get("VPN_INTERFACE", "wg0"),
    }]


def _trial_activation_kb(day_no: int) -> InlineKeyboardMarkup:
    if int(day_no) == 1:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔌 Подключить VPN", callback_data="nav:vpn")],
                [InlineKeyboardButton(text="📖 Как подключить", callback_data="vpn:guide")],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
            ]
        )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🌍 Открыть VPN", callback_data="nav:vpn")],
            [InlineKeyboardButton(text="💳 Купить подписку", callback_data="nav:pay")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
        ]
    )


def _family_upsell_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить место", callback_data="family:buy")],
            [InlineKeyboardButton(text="👤 Личный кабинет", callback_data="nav:cabinet")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
        ]
    )


async def _send_first_payment_family_upsell(bot: Bot, tg_id: int) -> None:
    try:
        await audit_send_message(
            bot,
            int(tg_id),
            "➕ <b>Можно добавить ещё одно место</b>\n\n"
            "Если хотите подключить второе устройство или дать доступ мужу, жене или ребёнку — "
            "добавьте ещё одно место в семейной группе.",
            kind="upsell_after_first_payment",
            reply_markup=_family_upsell_kb(),
            parse_mode="HTML",
        )
    except Exception:
        pass


async def _collect_referral_payment_notification(session, payment: Payment) -> dict | None:
    earning = await session.scalar(
        select(ReferralEarning)
        .where(ReferralEarning.payment_id == int(payment.id))
        .order_by(ReferralEarning.id.desc())
        .limit(1)
    )
    if not earning:
        return None
    referral = await session.scalar(
        select(Referral)
        .where(Referral.referred_tg_id == int(payment.tg_id))
        .order_by(Referral.id.desc())
        .limit(1)
    )
    return {
        "referrer_tg_id": int(earning.referrer_tg_id),
        "earned_rub": int(getattr(earning, "earned_rub", 0) or 0),
        "is_activation": bool(referral and int(getattr(referral, "first_payment_id", 0) or 0) == int(payment.id)),
        "available_at": getattr(earning, "available_at", None),
    }


async def _send_referral_payment_notifications(bot: Bot, payload: dict | None) -> None:
    if not payload:
        return
    referrer_tg_id = int(payload.get("referrer_tg_id") or 0)
    if referrer_tg_id <= 0:
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="👥 Открыть рефералку", callback_data="nav:referrals")]]
    )
    if payload.get("is_activation"):
        try:
            await audit_send_message(
                bot,
                referrer_tg_id,
                "🎉 Ваш друг оплатил подписку по реферальной ссылке. Реферал активирован.",
                kind="referral_paid",
                reply_markup=kb,
            )
        except Exception:
            pass
    hold_note = ""
    available_at = payload.get("available_at")
    if isinstance(available_at, datetime):
        hold_note = (
            f"\n\nДоступно к выводу после: "
            f"<b>{_ensure_tz(available_at).astimezone(AMSTERDAM_TZ).strftime('%d.%m.%Y %H:%M')}</b>."
        )
    try:
        await audit_send_message(
            bot,
            referrer_tg_id,
            f"💸 Начислено реферальное вознаграждение: <b>{int(payload.get('earned_rub') or 0)} ₽</b>.{hold_note}",
            kind="referral_earning_created",
            reply_markup=kb,
            parse_mode="HTML",
        )
    except Exception:
        pass


async def _send_referral_release_notifications(bot: Bot, released_rows: list[tuple[int, int, int]]) -> None:
    if not released_rows:
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="👥 Открыть рефералку", callback_data="nav:referrals")]]
    )
    for referrer_tg_id, items_cnt, total_rub in released_rows:
        try:
            suffix = f" за {int(items_cnt)} начисл." if int(items_cnt) > 1 else ""
            await audit_send_message(
                bot,
                int(referrer_tg_id),
                f"✅ Реферальный холд завершён. Доступно к выводу: <b>{int(total_rub)} ₽</b>{suffix}.",
                kind="referral_hold_released",
                reply_markup=kb,
                parse_mode="HTML",
            )
        except Exception:
            pass


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
    """Build admin kick report using the same broad logic as the manual admin screen.

    Important: this report intentionally includes overdue subscriptions even when the
    user is not currently in a Yandex family. The admin screen already shows such rows
    as requiring attention; the scheduled notification must not say "никого исключать не
    нужно" while those overdue rows exist.
    """
    now = _utcnow()

    YM = aliased(YandexMembership)
    latest_active_ids = (
        select(
            YM.tg_id.label("tg_id"),
            func.max(YM.id).label("id"),
        )
        .where(YM.removed_at.is_(None))
        .group_by(YM.tg_id)
        .subquery()
    )

    q = (
        select(Subscription, YM)
        .select_from(Subscription)
        .outerjoin(latest_active_ids, latest_active_ids.c.tg_id == Subscription.tg_id)
        .outerjoin(YM, YM.id == latest_active_ids.c.id)
        .where(
            Subscription.end_at.is_not(None),
            Subscription.end_at <= now,
        )
        .order_by(Subscription.end_at.asc(), Subscription.tg_id.asc())
        .limit(200)
    )
    rows = (await session.execute(q)).all()

    if not rows:
        return "Сегодня участников для исключения нет ✅"

    lines: list[str] = []
    lines.append("Сегодня требуют внимания следующие просроченные пользователи:\n")

    idx = 1
    for sub, m in rows:
        tg_id = int(sub.tg_id)

        pay_q = (
            select(func.max(Payment.paid_at))
            .where(Payment.tg_id == tg_id, Payment.status == "success")
        )
        paid_at = (await session.execute(pay_q)).scalar_one_or_none()

        peer_q = (
            select(VpnPeer)
            .where(VpnPeer.tg_id == tg_id, VpnPeer.is_active == True)
            .order_by(VpnPeer.id.desc())
            .limit(1)
        )
        peer = (await session.execute(peer_q)).scalar_one_or_none()
        vpn_status = "Включен" if peer else "Отключен"

        renewed = False
        try:
            renewed = _is_extended(sub.end_at, getattr(m, "coverage_end_at", None) if m else None)
        except Exception:
            renewed = False
        renewed_text = "Продлевалась" if renewed else "Не продлевалась"

        membership_state = "В семье" if m else "Не добавлен в семью"
        family_label = (getattr(m, "account_label", None) or getattr(m, "family_label", None) or "—") if m else "—"
        slot_index = getattr(m, "slot_index", None) if m else None

        lines.append(f"#{idx}")
        lines.append(f"Пользователь ID TG: {tg_id}")
        lines.append(f"Дата приобретения подписки на сервис: {_fmt_dt(paid_at) if paid_at else _fmt_dt(sub.start_at)}")
        lines.append(f"Дата окончания подписки на сервис: {_fmt_dt(sub.end_at)}")
        lines.append(f"Статус Яндекс семьи: {membership_state}")
        lines.append(f"Наименование семьи (label): {family_label}")
        lines.append(f"Номер слота: {slot_index if slot_index is not None else '—'}")
        lines.append(f"VPN: {vpn_status}")
        lines.append(f"Подписка: {renewed_text}")
        lines.append(f"Пользователь с нами: {_human_age(sub.start_at, now)}")
        lines.append("")
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
        # Admin reports are not audited per-user.
        await bot.send_message(owner_id, text_report)
    except Exception:
        pass


async def _job_reconcile_vpn_server_state() -> None:
    try:
        from app.services.vpn.service import vpn_service
    except Exception:
        return
    async with session_scope() as session:
        try:
            stats = await vpn_service.reconcile_live_peers(session)
            if stats.get("server_code_backfilled") or stats.get("reactivated"):
                await session.commit()
                log.info(
                    "vpn_reconcile_live_peers backfilled=%s reactivated=%s",
                    stats.get("server_code_backfilled", 0),
                    stats.get("reactivated", 0),
                )
            else:
                await session.rollback()
        except Exception:
            await session.rollback()
            log.exception("vpn_reconcile_live_peers_failed")




async def _job_reconcile_pending_platega_payments(bot: Bot) -> None:
    """Reconcile pending Platega payments after restarts or missed watcher tasks.

    Why:
    - The in-memory Platega watcher is best-effort and can be lost on deploy/restart.
    - Without reconciliation, money may be charged while subscription state stays pending,
      which later triggers false expiry reminders/auto-disable.

    Scope:
    - Normal VPN subscriptions (including winback variants).
    - LTE monthly payments.
    - Family payments are intentionally left to the existing family-specific flow.
    """
    if settings.payment_provider != "platega":
        return
    if not settings.platega_merchant_id or not settings.platega_secret:
        return

    from app.services.payments.platega import PlategaClient, PlategaError
    from app.services.vpn.service import vpn_service

    client = PlategaClient(merchant_id=settings.platega_merchant_id, secret=settings.platega_secret)
    now = _utcnow()
    notify_ok: list[tuple[int, str]] = []

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Payment)
                .where(
                    Payment.status == "pending",
                    Payment.provider_payment_id.is_not(None),
                    or_(
                        Payment.provider == "platega",
                        Payment.provider == "platega_lte",
                        Payment.provider.like("platega_winback_%"),
                    ),
                )
                .order_by(Payment.paid_at.asc(), Payment.id.asc())
                .limit(200)
            )
        ).scalars().all()

        changed = False
        referral_notifications: list[dict] = []
        first_payment_upsell_ids: set[int] = set()
        for pay in rows:
            provider_tid = str(pay.provider_payment_id or "").strip()
            if not provider_tid:
                continue

            try:
                st = await client.get_transaction_status(transaction_id=provider_tid)
            except PlategaError:
                continue
            except Exception:
                continue

            status = (st.status or "").upper()
            tg_id = int(pay.tg_id)

            if status in ("FAILED", "CANCELLED", "EXPIRED", "REJECTED"):
                pay.status = "failed"
                changed = True
                continue

            if status not in ("CONFIRMED", "SUCCESS", "PAID", "COMPLETED"):
                continue

            if (pay.provider or "") == "platega_lte":
                await lte_vpn_service.activate_paid_month(tg_id)
                pay.status = "success"
                changed = True
                notify_ok.append((tg_id, "✅ <b>Оплата подтверждена автоматически.</b>\n\nVPN LTE активирован."))
                continue

            sub = await get_subscription(session, tg_id)
            base = sub.end_at if sub.end_at and _ensure_tz(sub.end_at) > now else now
            add_months = int(getattr(pay, "period_months", 1) or 1)
            new_end = base + timedelta(days=int(getattr(pay, "period_days", 30) or 30))

            await extend_subscription(
                session,
                tg_id,
                months=add_months,
                days_legacy=int(getattr(pay, "period_days", 30) or 30),
                amount_rub=int(pay.amount),
                provider=str(pay.provider or "platega"),
                status="success",
                provider_payment_id=provider_tid,
            )

            try:
                if (pay.provider or "").startswith("platega_winback_"):
                    await set_app_setting_int(session, f"winback_promo_consumed:{tg_id}", 1)
            except Exception:
                pass

            try:
                await vpn_service.restore_expired_peers(session, tg_id, grace_hours=24)
            except Exception:
                pass

            pay.status = "success"
            try:
                await referral_service.on_successful_payment(session, pay)
                payload = await _collect_referral_payment_notification(session, pay)
                if payload:
                    referral_notifications.append(payload)
            except Exception:
                pass

            successful_before = int(
                await session.scalar(
                    select(func.count(Payment.id)).where(
                        Payment.tg_id == tg_id,
                        Payment.status == "success",
                        Payment.amount.is_not(None),
                        Payment.amount > 0,
                        Payment.id != int(pay.id),
                    )
                )
                or 0
            )
            if successful_before == 0:
                first_payment_upsell_ids.add(tg_id)

            sub.end_at = new_end
            sub.is_active = True
            sub.status = "active"

            try:
                if settings.yandex_enabled:
                    await yandex_service.rotate_membership_for_user_if_needed(session, tg_id=tg_id)
            except Exception:
                pass

            changed = True
            notify_ok.append((tg_id, "✅ <b>Оплата подтверждена автоматически.</b>\n\nПодписка активирована."))

        if changed:
            await session.commit()

    for tg_id, text_msg in notify_ok:
        try:
            await audit_send_message(
                bot,
                tg_id,
                text_msg,
                kind="payment_auto_reconciled",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")]]
                ),
            )
        except Exception:
            pass
    for payload in referral_notifications:
        await _send_referral_payment_notifications(bot, payload)
    for tg_id in sorted(first_payment_upsell_ids):
        await _send_first_payment_family_upsell(bot, tg_id)

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
                    await _job_reconcile_pending_platega_payments(bot)
                    await _job_expire_subscriptions(bot)
                    await _job_finalize_pending_vpn_migrations(bot)
                    await _job_prune_wg_peers()
                    await _job_reconcile_vpn_server_state()
                    await _job_prune_regionvpn_clients()
                    await _job_expire_lte_clients(bot)
                    await _job_poll_lte_connections(bot)
                    if settings.yandex_enabled:
                        await _job_rotate_yandex_invites(bot)
                    await _job_user_subscription_notifications(bot)
                    # VPN-only users (no YandexMembership) still need expiry reminders.
                    await _job_subscription_end_at_notifications(bot)
                    await _job_trial_activation_notifications(bot)
                    await _job_trial_expiring_notifications(bot)
                    await _job_trial_reengagement_notifications(bot)
                    await _job_family_group_expiring_notifications(bot)
                    await _job_lte_expiring_notifications(bot)
                    await _job_expire_family_groups(bot)
                    await _job_winback_discount_campaign(bot)
                    # Make pending referral earnings available when hold expires.
                    try:
                        due_rows = (
                            await session.execute(
                                select(
                                    ReferralEarning.referrer_tg_id,
                                    func.count(ReferralEarning.id),
                                    func.coalesce(func.sum(ReferralEarning.earned_rub), 0),
                                )
                                .where(
                                    ReferralEarning.status == "pending",
                                    ReferralEarning.available_at.is_not(None),
                                    ReferralEarning.available_at <= _utcnow(),
                                )
                                .group_by(ReferralEarning.referrer_tg_id)
                            )
                        ).all()
                        released = await referral_service.release_pending(session)
                        if released:
                            await session.commit()
                            await _send_referral_release_notifications(
                                bot,
                                [(int(r[0]), int(r[1]), int(r[2] or 0)) for r in due_rows if int(r[0] or 0) > 0],
                            )
                    except Exception:
                        pass
                    await _send_admin_kick_report(bot, force=False)
                finally:
                    await advisory_unlock(session)
        except Exception:
            log.exception("scheduler_loop_error")

        await asyncio.sleep(SLEEP_SECONDS)


async def _job_family_group_expiring_notifications(bot: Bot) -> None:
    """Warn owners about the nearest expiring paid family place."""
    now = _utcnow()
    now_local = now.astimezone(AMSTERDAM_TZ)
    if now_local.hour < 19:
        return

    async with session_scope() as session:
        groups = list((await session.execute(select(FamilyVpnGroup).where(FamilyVpnGroup.seats_total > 0).limit(2000))).scalars().all())
        if not groups:
            return

        changed = False

        for g in groups:
            owner = int(g.owner_tg_id)
            if not bool(getattr(g, 'billing_opt_in', False)):
                continue
            sub = await session.scalar(select(Subscription).where(Subscription.tg_id == owner).limit(1))
            if not sub or not sub.end_at or _ensure_tz(sub.end_at) <= now:
                continue
            rows = list((await session.execute(
                select(FamilyVpnProfile)
                .where(FamilyVpnProfile.owner_tg_id == owner)
                .order_by(FamilyVpnProfile.slot_no.asc())
            )).scalars().all())
            seats_total = int(g.seats_total or 0)
            nearest_slot = None
            nearest_end = None
            for p in rows[:seats_total]:
                end_at = _ensure_tz(getattr(p, 'expires_at', None)) if getattr(p, 'expires_at', None) else None
                if not end_at or end_at <= now:
                    continue
                if nearest_end is None or end_at < nearest_end:
                    nearest_end = end_at
                    nearest_slot = int(p.slot_no or 0)
            if nearest_end is None or nearest_slot is None:
                continue
            g.active_until = max((_ensure_tz(getattr(p, 'expires_at', None)) for p in rows[:seats_total] if getattr(p, 'expires_at', None) and _ensure_tz(getattr(p, 'expires_at', None)) > now), default=None)
            days_left = _days_until(nearest_end, now)
            if days_left not in (3, 2, 1):
                continue
            sent_key = f"family_warn_{days_left}d_sent:{owner}:{nearest_slot}:{nearest_end.date().isoformat()}"
            if bool(await get_app_setting_int(session, sent_key, default=0)):
                continue
            try:
                await bot.send_message(
                    owner,
                    (
                        f"⏳ Через <b>{days_left}</b> дн. закончится семейное место <b>№{nearest_slot}</b>.\n\n"
                        "Нажмите кнопку ниже, чтобы продлить места семейной группы."
                    ),
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(text="💳 Продлить семейные места", callback_data="family:renew_menu")],
                            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
                        ]
                    ),
                )
            finally:
                await set_app_setting_int(session, sent_key, 1)
                changed = True

        if changed:
            await session.commit()


async def _job_expire_family_groups(bot: Bot) -> None:
    """Disable expired family places individually without collapsing the whole family group."""
    now = _utcnow()
    async with session_scope() as session:
        groups = list((await session.execute(select(FamilyVpnGroup).where(FamilyVpnGroup.seats_total > 0).limit(1000))).scalars().all())
        if not groups:
            return
        try:
            from app.services.vpn.service import vpn_service
        except Exception:
            vpn_service = None

        changed = False
        notify_items: list[tuple[int, int, datetime]] = []

        for g in groups:
            owner = int(g.owner_tg_id)
            seats_total = int(g.seats_total or 0)
            rows = list((await session.execute(
                select(FamilyVpnProfile)
                .where(FamilyVpnProfile.owner_tg_id == owner)
                .order_by(FamilyVpnProfile.slot_no.asc())
            )).scalars().all())
            active_max = None
            for p in rows[:seats_total]:
                exp = getattr(p, 'expires_at', None)
                if exp is not None:
                    exp = _ensure_tz(exp)
                if exp and exp > now:
                    if active_max is None or exp > active_max:
                        active_max = exp
                    continue
                if not p.vpn_peer_id:
                    continue
                try:
                    peer = await session.get(VpnPeer, int(p.vpn_peer_id or 0))
                    if peer and peer.is_active:
                        if vpn_service:
                            try:
                                await vpn_service.remove_peer_for_server(public_key=peer.client_public_key, server_code=(peer.server_code or '').upper() or None)
                            except Exception:
                                try:
                                    await vpn_service.provider.remove_peer(peer.client_public_key)
                                except Exception:
                                    pass
                        peer.is_active = False
                        peer.revoked_at = now
                        peer.rotation_reason = f'family_slot_{int(p.slot_no or 0)}_expired'
                        changed = True
                        if exp is not None:
                            notify_items.append((owner, int(p.slot_no or 0), exp))
                except Exception:
                    pass
            g.active_until = active_max
            changed = True

        if changed:
            await session.commit()

    for owner, slot_no, exp in notify_items:
        try:
            async with session_scope() as session:
                sent_key = f"family_slot_expired_sent:{owner}:{slot_no}:{exp.date().isoformat()}"
                if bool(await get_app_setting_int(session, sent_key, default=0)):
                    continue
                await set_app_setting_int(session, sent_key, 1)
                await session.commit()
            await audit_send_message(
                bot,
                owner,
                (
                    f"⛔️ Семейное место <b>№{slot_no}</b> истекло.\n\n"
                    "Чтобы снова пользоваться этим профилем, продлите место в разделе семейной группы."
                ),
                kind='family_slot_expired',
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text='💳 Продлить места', callback_data='family:renew_menu')],
                        [InlineKeyboardButton(text='🏠 Главное меню', callback_data='nav:home')],
                    ]
                ),
            )
        except Exception:
            pass




async def _job_winback_discount_campaign(bot: Bot) -> None:
    """One-time winback discount campaign.

    Rules (per user, at most once in lifetime):
    - If subscription expired and user didn't pay for 2 days -> offer 69₽ for the first month.
    - If they don't accept within >3 days -> delete previous promo message and offer 29₽.
    - If they still don't accept for ~1.5 weeks -> send final 24h reminder and delete previous promo.

    We log send attempts via message_audit (including failures).
    """

    now = _utcnow()
    now_local = now.astimezone(AMSTERDAM_TZ)
    if now_local.hour < 19:
        return

    from app.services.message_audit import audit_send_message

    async with session_scope() as session:
        # eligible: subscription inactive and expired
        subs = list(
            (await session.execute(
                select(Subscription)
                .where(
                    Subscription.end_at.is_not(None),
                    Subscription.end_at <= now,
                )
                .order_by(Subscription.tg_id.asc())
                .limit(2000)
            )).scalars().all()
        )
        if not subs:
            return

        # current base price for strikethrough
        try:
            from app.repo import get_price_rub
            base_price = int(await get_price_rub(session))
        except Exception:
            base_price = 199

        changed = False

        for sub in subs:
            tg_id = int(sub.tg_id)

            # Skip if subscription is currently active
            if bool(getattr(sub, "is_active", False)) and sub.end_at and _ensure_tz(sub.end_at) > now:
                continue

            # Campaign only once per user lifetime.
            consumed = int(await get_app_setting_int(session, f"winback_promo_consumed:{tg_id}", default=0))
            if consumed:
                continue

            stage = int(await get_app_setting_int(session, f"winback_promo_stage:{tg_id}", default=0))
            # stage: 0 none, 1=69 sent, 2=29 sent, 3=final sent

            end_at = _ensure_tz(sub.end_at) if sub.end_at else None
            if not end_at:
                continue
            days_since = int((now - end_at).total_seconds() // 86400)

            # Stop if user already paid (any successful amount>0) after expiry.
            # We treat any successful paid purchase as conversion and then mark consumed.
            paid_q = (
                select(Payment.id)
                .where(
                    Payment.tg_id == tg_id,
                    Payment.status == "success",
                    Payment.amount.is_not(None),
                    Payment.amount > 0,
                    Payment.paid_at >= end_at,
                )
                .limit(1)
            )
            has_paid_after = (await session.execute(paid_q)).first() is not None
            if has_paid_after:
                await set_app_setting_int(session, f"winback_promo_consumed:{tg_id}", 1)
                await set_app_setting_int(session, f"winback_promo_stage:{tg_id}", 99)
                changed = True
                continue

            # Helper: delete previous promo message if we have its message_id stored.
            async def _delete_prev(event_key: str) -> None:
                try:
                    from app.db.models.message_audit import MessageAudit

                    mid = await session.scalar(
                        select(MessageAudit.message_id)
                        .where(
                            MessageAudit.tg_id == tg_id,
                            MessageAudit.event_key == event_key,
                            MessageAudit.message_id.is_not(None),
                        )
                        .order_by(MessageAudit.id.desc())
                        .limit(1)
                    )
                    if mid:
                        try:
                            await bot.delete_message(tg_id, int(mid))
                        except Exception:
                            pass
                except Exception:
                    return

            # Stage 1: 69₽ offer after 2 days
            if stage == 0 and days_since >= 2:
                msg = (
                    "🔥 <b>Только сегодня!</b>\n\n"
                    f"Цена подписки для вас: <s>{base_price} ₽</s> → <b>69 ₽</b> (только первый месяц)\n\n"
                    "Что вы получите:\n"
                    "• Стабильный высокоскоростной VPN\n"
                    "• Приглашение в семью Yandex Plus\n\n"
                    "Нажмите кнопку ниже, чтобы перейти к оплате." 
                )
                await audit_send_message(
                    bot,
                    tg_id,
                    msg,
                    kind="winback_69",
                    reply_markup=_winback_kb(amount_rub=69),
                    parse_mode="HTML",
                )
                await set_app_setting_int(session, f"winback_promo_stage:{tg_id}", 1)
                changed = True
                continue

            # Stage 2: 29₽ offer if >3 days passed since stage1 (i.e. expiry+5 days)
            if stage == 1 and days_since >= 5:
                await _delete_prev("winback_69")
                msg = (
                    "🔥 <b>Супер-скидка!</b>\n\n"
                    f"Цена подписки для вас: <s>{base_price} ₽</s> → <s>69 ₽</s> → <b>29 ₽</b> (только первый месяц)\n\n"
                    "Что вы получите:\n"
                    "• Стабильный высокоскоростной VPN\n"
                    "• Приглашение в семью Yandex Plus\n\n"
                    "Нажмите кнопку ниже, чтобы перейти к оплате." 
                )
                await audit_send_message(
                    bot,
                    tg_id,
                    msg,
                    kind="winback_29",
                    reply_markup=_winback_kb(amount_rub=29),
                    parse_mode="HTML",
                )
                await set_app_setting_int(session, f"winback_promo_stage:{tg_id}", 2)
                changed = True
                continue

            # Stage 3: final reminder ~1.5 weeks after they ignored (expiry+16 days)
            if stage == 2 and days_since >= 16:
                await _delete_prev("winback_29")
                msg = (
                    "⏳ <b>Последний шанс!</b>\n\n"
                    "Через <b>24 часа</b> цена вернётся к обычной. "
                    "Если вы хотите сохранить выгоду — оформите подписку сейчас.\n\n"
                    f"Сейчас для вас: <s>{base_price} ₽</s> → <s>69 ₽</s> → <b>29 ₽</b> (первый месяц)\n\n"
                    "Что вы получите:\n"
                    "• Стабильный высокоскоростной VPN\n"
                    "• Приглашение в семью Yandex Plus\n"
                )
                await audit_send_message(
                    bot,
                    tg_id,
                    msg,
                    kind="winback_final",
                    reply_markup=_winback_kb(amount_rub=29),
                    parse_mode="HTML",
                )
                await set_app_setting_int(session, f"winback_promo_stage:{tg_id}", 3)
                changed = True
                continue

            # If stage 3 already sent -> mark consumed to prevent repeats in future cycles.
            if stage == 3 and days_since >= 18:
                await set_app_setting_int(session, f"winback_promo_consumed:{tg_id}", 1)
                await set_app_setting_int(session, f"winback_promo_stage:{tg_id}", 99)
                changed = True

        if changed:
            await session.commit()


async def _job_expire_subscriptions(bot: Bot) -> None:
    async with session_scope() as session:
        from app.repo import utcnow, deactivate_peers
        from app.db.models.vpn_peer import VpnPeer
        from app.services.vpn.service import VPNService
        servers_by_code = {s.get("code"): s for s in _get_scheduler_vpn_servers()}

        # Best-effort: may fail if WG env vars are not configured.
        try:
            vpn_svc: VPNService | None = VPNService()
        except Exception:
            vpn_svc = None

        now = utcnow()
        expired = await list_expired_subscriptions(session, now)
        if not expired:
            return

        expired_ids: list[int] = []
        for sub in expired:
            tg_id = sub.tg_id

            # Disable WireGuard access on the server immediately, but do NOT
            # permanently purge the peer yet. This allows re-enabling the same
            # peer (same config) if the user pays within the grace window.
            peers: list[VpnPeer] = []
            try:
                rows = await session.execute(select(VpnPeer).where(VpnPeer.tg_id == tg_id, VpnPeer.is_active == True))
                peers = list(rows.scalars().all())
            except Exception:
                peers = []

            if vpn_svc and peers:
                for p in peers:
                    try:
                        code = (p.server_code or "").upper() or None
                        srv = servers_by_code.get(code) if code else None
                        if srv and srv.get("host") and srv.get("user"):
                            # "Disable" = remove from live wg interface.
                            await vpn_svc.remove_peer_for_server(
                                public_key=p.client_public_key,
                                host=str(srv["host"]),
                                port=int(srv.get("port") or 22),
                                user=str(srv["user"]),
                                password=srv.get("password"),
                                interface=str(srv.get("interface") or "wg0"),
                            )
                        else:
                            await vpn_svc.provider.remove_peer(p.client_public_key)
                    except Exception:
                        log.exception(
                            "vpn_peer_remove_on_expire_failed tg_id=%s peer_id=%s",
                            tg_id,
                            getattr(p, "id", None),
                        )

            await set_subscription_expired(session, tg_id)
            # Mark peers as inactive with an "expired" reason so we can restore
            # them on payment (within 24h) and later purge after 24h.
            await deactivate_peers(session, tg_id, reason="expired")
            expired_ids.append(tg_id)

            # Manual Yandex process: owner will remove user from the family.
            try:
                await audit_send_message(
                    bot,
                    tg_id,
                    "⛔️ Подписка истекла.\n"
                    "• Доступ к VPN отключён.\n"
                    "• Вы будете исключены из семейной подписки Yandex Plus, так как срок подписки истёк.",
                    kind="sub_expired",
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


async def _job_prune_wg_peers() -> None:
    """After 24 hours of subscription expiration, mark WG peers as purged.

    We disable peers immediately on expiration (remove from wg interface) so the
    config stops working. If the user does NOT renew within 24 hours, we mark
    those peers as "expired_purged" so they won't be automatically restored.
    """

    cutoff = _utcnow() - timedelta(days=1)

    async with session_scope() as session:
        # Find peers disabled due to expiration for > 24h.
        q = (
            select(VpnPeer)
            .where(
                VpnPeer.is_active.is_(False),
                VpnPeer.rotation_reason == "expired",
                VpnPeer.revoked_at.is_not(None),
                VpnPeer.revoked_at < cutoff,
            )
            .limit(500)
        )
        peers = list((await session.execute(q)).scalars().all())
        if not peers:
            return

        # Best-effort: try removing from server again (idempotent), then mark purged.
        try:
            from app.services.vpn.service import vpn_service
        except Exception:
            vpn_service = None

        for p in peers:
            try:
                if vpn_service:
                    try:
                        await vpn_service.provider.remove_peer(p.client_public_key)
                    except Exception:
                        pass
                p.rotation_reason = "expired_purged"
            except Exception:
                pass

        await session.commit()


async def _job_prune_regionvpn_clients() -> None:
    """After 24 hours of inactivity (expired subscription), remove the client from Xray.

    This keeps the same config usable for 24h after expiration (while blocked),
    but eventually frees up the server state.
    """

    # Backwards compatible safety:
    # - In some deployments RegionVPN isn't configured at all.
    # - Older Settings may expose region_* fields instead of regionvpn_*.
    # This job must NEVER crash the scheduler loop.
    if not getattr(settings, "regionvpn_enabled", False):
        return

    # If required RegionVPN settings are missing, skip silently.
    required = [
        "regionvpn_ssh_host",
        "regionvpn_ssh_user",
        "regionvpn_ssh_key_path",
        "regionvpn_xray_config_path",
        "regionvpn_xray_restart_command",
        "regionvpn_vless_tag",
        "regionvpn_vless_port",
        "regionvpn_vless_sni",
        "regionvpn_vless_pbk",
        "regionvpn_vless_sid",
        "regionvpn_vless_flow",
    ]
    if any(not hasattr(settings, k) for k in required):
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
            await audit_send_message(
                tg_id,
                "🔁 Пора перейти в новую семейную подписку Yandex Plus.\n\n"
                "Откройте 🟡 Yandex Plus и нажмите «Открыть приглашение», или используйте ссылку ниже:",
                kind="yandex_rotate",
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
                await audit_send_message(
                    bot,
                    tg_id,
                    "🔁 Пора перейти в новую семейную подписку Yandex Plus.",
                    kind="yandex_rotate",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(text="🟡 Yandex Plus", callback_data="nav:yandex")],
                            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
                        ]
                    ),
                )
            except Exception:
                pass


async def _job_trial_activation_notifications(bot: Bot) -> None:
    """D1/D2 nudges during trial to push first useful VPN action."""
    now = _utcnow()
    now_local = now.astimezone(AMSTERDAM_TZ)
    if now_local.hour < 12:
        return

    async with session_scope() as session:
        q = (
            select(Subscription)
            .where(
                Subscription.is_active.is_(True),
                Subscription.end_at.is_not(None),
                Subscription.end_at > now,
            )
            .order_by(Subscription.tg_id.asc())
            .limit(1000)
        )
        rows = (await session.execute(q)).scalars().all()
        if not rows:
            return

        changed = False
        for sub in rows:
            tg_id = int(sub.tg_id)
            trial_used = bool(await get_app_setting_int(session, f"trial_used:{tg_id}", default=0))
            if not trial_used:
                continue
            paid_q = (
                select(Payment.id)
                .where(
                    Payment.tg_id == tg_id,
                    Payment.status == "success",
                    Payment.amount.is_not(None),
                    Payment.amount > 0,
                )
                .limit(1)
            )
            if (await session.execute(paid_q)).first() is not None:
                continue
            trial_end_ts = await get_app_setting_int(session, f"trial_end_ts:{tg_id}", default=0)
            if trial_end_ts <= 0:
                continue
            trial_end_at = datetime.fromtimestamp(int(trial_end_ts), tz=timezone.utc)
            if trial_end_at <= now:
                continue
            trial_started_at = trial_end_at - timedelta(days=5)
            days_since_start = int((now - trial_started_at).total_seconds() // 86400)
            if 1 <= days_since_start < 2:
                due_day = 1
            elif 2 <= days_since_start < 3:
                due_day = 2
            else:
                continue
            sent_key = f"trial_d{due_day}_sent:{tg_id}"
            if bool(await get_app_setting_int(session, sent_key, default=0)):
                continue
            if due_day == 1:
                text_msg = (
                    "🔌 <b>VPN уже активен</b>\n\n"
                    "Если ещё не подключились — сделайте это сейчас и проверьте Telegram, YouTube и сайты, которые у вас обычно тормозят. "
                    "Пара минут на настройку — и вы сразу увидите разницу."
                )
            else:
                text_msg = (
                    "🌍 <b>Проверьте сервис в обычном режиме</b>\n\n"
                    "Откройте Telegram, звонки, медиа и проблемные сайты. Важно найти для себя рабочий сценарий ещё во время trial, "
                    "чтобы пользоваться VPN каждый день без лишней настройки."
                )
            try:
                await audit_send_message(
                    bot,
                    tg_id,
                    text_msg,
                    kind=f"trial_d{due_day}",
                    reply_markup=_trial_activation_kb(due_day),
                    parse_mode="HTML",
                )
                await set_app_setting_int(session, sent_key, 1)
                changed = True
            except Exception:
                pass
        if changed:
            await session.commit()


def _trial_expiring_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Купить подписку", callback_data="nav:pay")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
        ]
    )


async def _job_trial_expiring_notifications(bot: Bot) -> None:
    """Warn active trial users 3/2/1 days before trial end at an evening local time.

    We send once per milestone, only after 19:00 Europe/Amsterdam (when users are
    more likely to be on their phones), and only to users who started a trial and
    still have never made a paid purchase.
    """
    now = _utcnow()
    now_local = now.astimezone(AMSTERDAM_TZ)

    # Wait until evening local time to avoid noisy daytime pings.
    if now_local.hour < 19:
        return

    async with session_scope() as session:
        q = (
            select(Subscription)
            .where(
                Subscription.is_active.is_(True),
                Subscription.end_at.is_not(None),
                Subscription.end_at > now,
            )
            .order_by(Subscription.tg_id.asc())
            .limit(1000)
        )
        rows = (await session.execute(q)).scalars().all()
        if not rows:
            return

        changed = False

        for sub in rows:
            tg_id = int(sub.tg_id)

            trial_used = bool(await get_app_setting_int(session, f"trial_used:{tg_id}", default=0))
            if not trial_used:
                continue

            paid_q = (
                select(Payment.id)
                .where(
                    Payment.tg_id == tg_id,
                    Payment.status == "success",
                    Payment.amount.is_not(None),
                    Payment.amount > 0,
                )
                .limit(1)
            )
            has_paid = (await session.execute(paid_q)).first() is not None
            if has_paid:
                continue

            trial_end_ts = await get_app_setting_int(session, f"trial_end_ts:{tg_id}", default=0)
            if trial_end_ts > 0:
                trial_end_at = datetime.fromtimestamp(int(trial_end_ts), tz=timezone.utc)
            elif sub.end_at is not None:
                trial_end_at = _ensure_tz(sub.end_at)
            else:
                continue

            if trial_end_at <= now:
                continue

            days_left = _days_until(trial_end_at, now)
            if days_left not in (3, 2, 1):
                continue

            sent_key = f"trial_warn_{days_left}d_sent:{tg_id}"
            if bool(await get_app_setting_int(session, sent_key, default=0)):
                continue

            if days_left == 3:
                text_msg = (
                    "⏳ Ваш пробный период закончится через 3 дня.\n\n"
                    "Чтобы не потерять доступ к VPN и Yandex Plus, можно заранее оформить полную подписку."
                )
            elif days_left == 2:
                text_msg = (
                    "⚠️ До окончания пробного периода осталось 2 дня.\n\n"
                    "Сохраните доступ к VPN и Yandex Plus — продлите подписку заранее."
                )
            else:
                text_msg = (
                    "🚨 Завтра закончится ваш пробный период.\n\n"
                    "Чтобы сервис продолжил работать без перерыва, оформите полную подписку уже сейчас."
                )

            try:
                await audit_send_message(
                    bot,
                    tg_id,
                    text_msg,
                    kind=f"trial_warn_{days_left}d",
                    reply_markup=_trial_expiring_kb(),
                )
                if days_left == 1:
                    await audit_send_message(
                        bot,
                        tg_id,
                        "➕ <b>Дополнительно:</b> после активации основной подписки вы сможете добавить ещё одно место для второго устройства или члена семьи через семейную группу — это выгоднее, чем оформлять отдельную подписку, примерно на 50%.",
                        kind="upsell_trial_end",
                        reply_markup=InlineKeyboardMarkup(
                            inline_keyboard=[
                                [InlineKeyboardButton(text="💳 Купить подписку", callback_data="nav:pay")],
                                [InlineKeyboardButton(text="👨‍👩‍👧‍👦 Семейная группа", callback_data="vpn:family")],
                            ]
                        ),
                        parse_mode="HTML",
                    )
                await set_app_setting_int(session, sent_key, 1)
                changed = True
            except Exception:
                pass

        if changed:
            await session.commit()


async def _job_user_subscription_notifications(bot: Bot) -> None:
    """User notifications based on coverage_end_at and extension state.

    Rules:
    - This job is ONLY about Yandex invite rotation notifications.
    - Subscription expiry reminders (7/3/1) are sent by
      _job_subscription_end_at_notifications based on Subscription.end_at for ALL
      users (VPN-only and VPN+Yandex) so users keep getting reminders after renewals.
    - If renewed: send 1-day notice about new invite tomorrow.
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
                        await audit_send_message(
                            bot,
                            int(m.tg_id),
                            "ℹ️ Завтра вам будет выдано новое приглашение в семейную подписку Yandex Plus.\n\n"
                            "Это связано со сменой аккаунта. Никаких действий сейчас не требуется.",
                            kind="yandex_invite_tomorrow",
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

            # For non-renewed users, expiry reminders are handled by
            # _job_subscription_end_at_notifications (based on Subscription.end_at).
            # We intentionally do nothing here to avoid duplicate notifications.

        if changed:
            await session.commit()


async def _job_expire_lte_clients(bot: Bot) -> None:
    now = _utcnow()
    async with session_scope() as session:
        q = (
            select(LteVpnClient, Subscription)
            .outerjoin(Subscription, Subscription.tg_id == LteVpnClient.tg_id)
            .where(LteVpnClient.is_enabled.is_(True))
            .limit(1000)
        )
        rows = (await session.execute(q)).all()
        if not rows:
            return

        to_disable: list[int] = []
        for row, sub in rows:
            main_active = bool(sub and sub.end_at and _ensure_tz(sub.end_at) > now)
            paid_lte_expired = bool(row.cycle_anchor_end_at and _ensure_tz(row.cycle_anchor_end_at) <= now)
            if (not main_active) or paid_lte_expired:
                row.is_enabled = False
                row.updated_at = now
                to_disable.append(int(row.tg_id))

        if not to_disable:
            return
        await session.commit()

    for tg_id in to_disable:
        try:
            await lte_vpn_service.disable_remote_client(tg_id)
        except Exception:
            log.exception("lte_disable_remote_failed tg_id=%s", tg_id)
        try:
            await audit_send_message(
                bot,
                tg_id,
                "⛔️ Срок действия VPN LTE закончился, поэтому профиль отключён.\n\nЧтобы снова пользоваться LTE-профилем, откройте раздел «📶 VPN LTE» и активируйте его заново.",
                kind="lte_expired",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="📶 Открыть VPN LTE", callback_data="vpn:lte")],
                        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
                    ]
                ),
            )
        except Exception:
            pass


async def _job_lte_expiring_notifications(bot: Bot) -> None:
    now = _utcnow()
    from app.db.models import LteVpnClient

    async with session_scope() as session:
        q = (
            select(LteVpnClient)
            .where(
                LteVpnClient.is_enabled.is_(True),
                LteVpnClient.cycle_anchor_end_at.is_not(None),
                LteVpnClient.cycle_anchor_end_at > now,
            )
            .order_by(LteVpnClient.tg_id.asc())
            .limit(1000)
        )
        rows = (await session.execute(q)).scalars().all()
        if not rows:
            return

        changed = False
        for row in rows:
            tg_id = int(row.tg_id)
            end_at = _ensure_tz(row.cycle_anchor_end_at)
            days_left = _days_until(end_at, now)
            if days_left not in (7, 3, 1):
                continue

            sent_key = f"lte_warn_{days_left}d_sent:{tg_id}:{end_at.date().isoformat()}"
            if bool(await get_app_setting_int(session, sent_key, default=0)):
                continue

            if days_left == 7:
                text_msg = (
                    "⏳ Через 7 дней закончится активация VPN LTE.\n\n"
                    "Чтобы LTE-профиль продолжал работать без перерыва, продлите его заранее."
                )
            elif days_left == 3:
                text_msg = (
                    "⚠️ Осталось 3 дня до окончания активации VPN LTE.\n\n"
                    "Продлите LTE заранее, чтобы профиль не отключился."
                )
            else:
                text_msg = (
                    "🚨 Завтра закончится активация VPN LTE.\n\n"
                    "Продлите LTE сейчас, чтобы доступ не отключился."
                )

            try:
                await audit_send_message(
                    bot,
                    tg_id,
                    text_msg,
                    kind=f"lte_warn_{days_left}d",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(text="💳 Оплатить LTE", callback_data="vpn:lte")],
                            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
                        ]
                    ),
                )
                await set_app_setting_int(session, sent_key, 1)
                changed = True
            except Exception:
                try:
                    await set_app_setting_int(session, sent_key, 1)
                    changed = True
                except Exception:
                    pass

        if changed:
            await session.commit()


async def _job_subscription_end_at_notifications(bot: Bot) -> None:
    """Send 7/3/1 day reminders based on Subscription.end_at for VPN users.

    Why:
    - _job_user_subscription_notifications is tied to YandexMembership.coverage_end_at.
    - VPN-only users may not have YandexMembership rows at all, so they would receive
      no pre-expiry reminders.

    Rules:
    - Only for active subscriptions with end_at in the future.
    - Applies to ALL users (VPN-only and VPN+Yandex). YandexMembership-based job
      only sends the "invite tomorrow" notice, not expiry reminders.
    - Send once per milestone per конкретную дату окончания (end_at), stored in
      app_settings.
    - Send after 19:00 Europe/Amsterdam to hit evening hours.
    """

    now = _utcnow()
    now_local = now.astimezone(AMSTERDAM_TZ)
    if now_local.hour < 19:
        return

    async with session_scope() as session:
        subs = (
            await session.execute(
                select(Subscription)
                .where(
                    Subscription.is_active.is_(True),
                    Subscription.end_at.is_not(None),
                    Subscription.end_at > now,
                )
                .order_by(Subscription.end_at.asc())
                .limit(2000)
            )
        ).scalars().all()
        if not subs:
            return

        changed = False

        for sub in subs:
            tg_id = int(sub.tg_id)


            # Trial users get 3/2/1 day reminders via _job_trial_expiring_notifications.
            # Skip them here to avoid sending 7/3/1 for a 5-day trial.
            try:
                trial_used = bool(await get_app_setting_int(session, f"trial_used:{tg_id}", default=0))
            except Exception:
                trial_used = False

            if trial_used:
                # If the user has any paid purchase, treat as non-trial for reminders.
                has_paid = False
                try:
                    paid_q = (
                        select(Payment.id)
                        .where(
                            Payment.tg_id == tg_id,
                            Payment.status == "success",
                            Payment.amount.is_not(None),
                            Payment.amount > 0,
                        )
                        .limit(1)
                    )
                    has_paid = (await session.execute(paid_q)).first() is not None
                except Exception:
                    has_paid = False

                if not has_paid:
                    # Identify likely trial subscription by explicit trial_end_ts or short duration.
                    try:
                        trial_end_ts = await get_app_setting_int(session, f"trial_end_ts:{tg_id}", default=0)
                    except Exception:
                        trial_end_ts = 0

                    is_short = False
                    try:
                        if sub.start_at and sub.end_at:
                            is_short = (_ensure_tz(sub.end_at) - _ensure_tz(sub.start_at)) <= timedelta(days=6)
                    except Exception:
                        is_short = False

                    if trial_end_ts > 0 or is_short:
                        continue

            # Do NOT skip Yandex users here: they should also receive expiry
            # reminders based on Subscription.end_at.

            end_at = _ensure_tz(sub.end_at)
            days_left = _days_until(end_at, now)
            if days_left not in (7, 3, 1):
                continue

            # Tie "sent" flags to a конкретной дате окончания подписки.
            end_key = end_at.astimezone(AMSTERDAM_TZ).strftime("%Y-%m-%d")
            sent_key = f"sub_end_warn_{days_left}d_sent:{tg_id}:{end_key}"
            if bool(await get_app_setting_int(session, sent_key, default=0)):
                continue

            if days_left == 7:
                text_msg = (
                    "⏳ Через 7 дней закончится ваша подписка на VPN.\n\n"
                    "Продлите подписку, чтобы продолжать пользоваться сервисом без перерыва."
                )
                kind = "sub_warn_7d"
            elif days_left == 3:
                text_msg = (
                    "⚠️ Осталось 3 дня до окончания подписки на VPN.\n\n"
                    "Продлите подписку заранее, чтобы не потерять доступ."
                )
                kind = "sub_warn_3d"
            else:
                text_msg = (
                    "🚨 Завтра закончится ваша подписка на VPN.\n\n"
                    "Продлите подписку сейчас, чтобы доступ не отключился."
                )
                kind = "sub_warn_1d"

            try:
                await audit_send_message(
                    bot,
                    tg_id,
                    text_msg,
                    kind=kind,
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(text="💳 Оплата", callback_data="nav:pay")],
                            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
                        ]
                    ),
                )
                await set_app_setting_int(session, sent_key, 1)
                changed = True
            except Exception:
                # audit_send_message already logs SEND_FAILED. Still mark the
                # attempt so we don't spam every loop.
                try:
                    await set_app_setting_int(session, sent_key, 1)
                    changed = True
                except Exception:
                    pass

        if changed:
            await session.commit()


TRIAL_REENGAGEMENT_DAY_MARKS = [1, 3, 7, 14, 30]


def _trial_reengagement_text() -> str:
    return (
        "Вы уверены, что больше не хотите продлять подписку?\n\n"
        "Люди получают подписку яндекс плюс и получает высокоскоростной впн, "
        "работающий всегда стабильно.\n\n"
        "Нажмите кнопку ниже чтобы перейти к оплате подписки."
    )


def _trial_reengagement_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Купить подписку", callback_data="nav:pay")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")],
        ]
    )


async def _job_trial_reengagement_notifications(bot: Bot) -> None:
    """Re-engagement drip for users whose 5-day trial expired and who never paid.

    Schedule for 6 months after trial end (fixed checkpoints):
    1st month — ~2 times/week, then gradually less often.
    """
    now = _utcnow()

    async with session_scope() as session:
        q = (
            select(Subscription)
            .where(
                Subscription.end_at.is_not(None),
                Subscription.end_at <= now,
            )
            .order_by(Subscription.tg_id.asc())
            .limit(1000)
        )
        rows = (await session.execute(q)).scalars().all()
        if not rows:
            return

        changed = False

        for sub in rows:
            tg_id = int(sub.tg_id)

            # Only users who used trial and still have never made a paid purchase.
            trial_used = bool(await get_app_setting_int(session, f"trial_used:{tg_id}", default=0))
            if not trial_used:
                continue

            paid_q = (
                select(Payment.id)
                .where(
                    Payment.tg_id == tg_id,
                    Payment.status == "success",
                    Payment.amount.is_not(None),
                    Payment.amount > 0,
                )
                .limit(1)
            )
            has_paid = (await session.execute(paid_q)).first() is not None
            if has_paid:
                continue

            if bool(sub.is_active) and sub.end_at and _ensure_tz(sub.end_at) > now:
                continue

            trial_end_ts = await get_app_setting_int(session, f"trial_end_ts:{tg_id}", default=0)
            if trial_end_ts > 0:
                trial_end_at = datetime.fromtimestamp(int(trial_end_ts), tz=timezone.utc)
            else:
                trial_pay_q = (
                    select(Payment.paid_at)
                    .where(
                        Payment.tg_id == tg_id,
                        Payment.status == "success",
                        Payment.provider == "trial",
                    )
                    .order_by(Payment.id.desc())
                    .limit(1)
                )
                trial_paid_at = (await session.execute(trial_pay_q)).scalar_one_or_none()
                if trial_paid_at is None and sub.end_at is not None:
                    trial_paid_at = _ensure_tz(sub.end_at) - timedelta(days=5)
                if trial_paid_at is None:
                    continue
                trial_end_at = _ensure_tz(trial_paid_at) + timedelta(days=5)
                await set_app_setting_int(session, f"trial_end_ts:{tg_id}", int(trial_end_at.timestamp()))
                changed = True

            if trial_end_at > now:
                continue

            days_since_end = max(0, int((now - trial_end_at).total_seconds() // 86400))
            stage_key = f"trial_reengagement_stage:{tg_id}"
            sent_stage = int(await get_app_setting_int(session, stage_key, default=0))

            due_stage = sent_stage
            for idx_mark, day_mark in enumerate(TRIAL_REENGAGEMENT_DAY_MARKS, start=1):
                if days_since_end >= day_mark:
                    due_stage = idx_mark
                else:
                    break

            if due_stage <= sent_stage:
                continue

            try:
                await audit_send_message(
                    bot,
                    tg_id,
                    _trial_reengagement_text(days_since_end),
                    kind=f"trial_reengage_{due_stage}",
                    reply_markup=_trial_reengagement_kb(),
                )
                await set_app_setting_int(session, stage_key, due_stage)
                changed = True
            except Exception:
                pass

        if changed:
            await session.commit()


async def _job_poll_lte_connections(bot: Bot) -> None:
    if not settings.lte_enabled:
        return
    try:
        result = await lte_vpn_service.poll_new_connections()
    except Exception:
        log.exception("lte_poll_failed")
        return
    # LTE connection notifications disabled: users complained about noisy alerts.
    # Keep polling for anti-sharing detection and strict disables below.
    for tg_id in result.warned_ids:
        try:
            await bot.send_message(
                int(tg_id),
                "⚠️ Обнаружены признаки одновременного использования VPN LTE с нескольких сетей или устройств. Пожалуйста, используйте LTE-конфиг только лично и на одном активном подключении.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")]]
                ),
            )
        except Exception:
            pass
    for tg_id in result.strict_disabled_ids:
        try:
            await bot.send_message(
                int(tg_id),
                "⛔️ VPN LTE временно отключён из-за признаков передачи доступа другим людям. Зайдите в раздел VPN LTE и получите конфиг заново, если это были вы и произошла ошибка.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")]]
                ),
            )
        except Exception:
            pass
