from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models.app_setting import AppSetting
from app.db.models.content_request import ContentRequest
from app.db.models.payment import Payment
from app.db.models.subscription import Subscription
from app.db.models.user import User
from app.db.models.vpn_peer import VpnPeer
from app.db.models.yandex_account import YandexAccount
from app.db.models.yandex_invite_slot import YandexInviteSlot


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def pick_account(session: AsyncSession, need_cover_until: datetime | None = None) -> YandexAccount | None:
    q = select(YandexAccount).where(YandexAccount.status == "active")
    if need_cover_until is not None:
        q = q.where((YandexAccount.plus_end_at.is_(None)) | (YandexAccount.plus_end_at >= need_cover_until))
    q = q.order_by(YandexAccount.id.asc())
    rows = (await session.execute(q)).scalars().all()
    if not rows:
        return None
    ids = [int(acc.id) for acc in rows]
    free_slot_account_ids = {
        int(x)
        for x in (
            await session.scalars(
                select(YandexInviteSlot.yandex_account_id).where(
                    and_(YandexInviteSlot.yandex_account_id.in_(ids), YandexInviteSlot.status == "free")
                )
            )
        ).all()
    }
    for acc in rows:
        if int(acc.id) in free_slot_account_ids:
            return acc
    return None


async def get_app_setting_int(session: AsyncSession, key: str, default: int | None = None) -> int | None:
    row = await session.get(AppSetting, key)
    if row is None or row.int_value is None:
        return default
    return int(row.int_value)


async def set_app_setting_int(session: AsyncSession, key: str, value: int | None) -> None:
    row = await session.get(AppSetting, key)
    if row is None:
        row = AppSetting(key=key, int_value=value)
        row.touch()
        session.add(row)
    else:
        row.int_value = value
        row.touch()


async def ensure_user(
    session: AsyncSession,
    tg_id: int,
    username: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
) -> User:
    user = await session.get(User, int(tg_id))
    if user is None:
        user = User(
            tg_id=int(tg_id),
            tg_username=username,
            first_name=first_name,
            last_name=last_name,
        )
        session.add(user)
    else:
        if username is not None:
            user.tg_username = username
        if first_name is not None:
            user.first_name = first_name
        if last_name is not None:
            user.last_name = last_name
    return user


async def get_subscription(session: AsyncSession, tg_id: int) -> Subscription:
    sub = await session.get(Subscription, int(tg_id))
    if sub is None:
        sub = Subscription(tg_id=int(tg_id), is_active=False, status="inactive")
        session.add(sub)
        await session.flush()
    return sub


async def extend_subscription(
    session: AsyncSession,
    tg_id: int,
    *,
    period_days: int | None = None,
    period_months: int | None = None,
    months: int | None = None,
    days_legacy: int | None = None,
    amount_rub: int | None = None,
    provider: str | None = None,
    status: str | None = None,
    provider_payment_id: str | None = None,
) -> Subscription:
    """Extend user subscription.

    Supports both the new argument names (period_days/period_months)
    and the legacy names still used by handlers/admin flows
    (months/days_legacy + payment metadata).
    """
    sub = await get_subscription(session, tg_id)
    now = utcnow()

    # Backward-compatible argument resolution.
    days = int(period_days or days_legacy or 0)
    resolved_months = int(period_months if period_months is not None else (months or 0))
    if days <= 0:
        months_for_days = resolved_months or int(settings.period_months or 1)
        days = 30 * max(1, months_for_days)

    base = sub.end_at if (sub.end_at and sub.end_at > now) else now
    sub.start_at = sub.start_at or now
    sub.end_at = base + timedelta(days=days)
    sub.is_active = True
    sub.status = "active"

    # Legacy compatibility: a lot of flows still expect extend_subscription()
    # to also persist a payment row, so keep that behavior when metadata is passed.
    if any(v is not None for v in (amount_rub, provider, status, provider_payment_id)):
        existing_payment = None
        if provider_payment_id:
            existing_payment = await session.scalar(
                select(Payment).where(Payment.provider_payment_id == provider_payment_id).limit(1)
            )
        if existing_payment is None:
            payment = Payment(
                tg_id=int(tg_id),
                amount=int(amount_rub or 0),
                currency="RUB",
                provider=str(provider or "mock"),
                status=str(status or "success"),
                period_days=int(days),
                period_months=max(0, resolved_months),
                provider_payment_id=provider_payment_id,
            )
            session.add(payment)
        else:
            existing_payment.tg_id = int(tg_id)
            existing_payment.amount = int(amount_rub if amount_rub is not None else existing_payment.amount)
            existing_payment.currency = "RUB"
            if provider is not None:
                existing_payment.provider = str(provider)
            if status is not None:
                existing_payment.status = str(status)
            existing_payment.period_days = int(days)
            existing_payment.period_months = max(0, resolved_months)

    return sub


async def list_expired_subscriptions(session: AsyncSession, now: datetime | None = None) -> list[Subscription]:
    now = now or utcnow()
    return (
        await session.execute(
            select(Subscription).where(
                Subscription.is_active.is_(True),
                Subscription.end_at.is_not(None),
                Subscription.end_at <= now,
            )
        )
    ).scalars().all()


async def set_subscription_expired(session: AsyncSession, tg_id: int) -> None:
    sub = await session.get(Subscription, int(tg_id))
    if sub is not None:
        sub.is_active = False
        sub.status = "expired"


async def deactivate_peers(session: AsyncSession, tg_id: int, reason: str | None = None) -> None:
    now = utcnow()
    values: dict[str, object] = {"is_active": False, "revoked_at": now}
    if reason is not None:
        values["rotation_reason"] = str(reason)
    await session.execute(
        update(VpnPeer)
        .where(VpnPeer.tg_id == int(tg_id), VpnPeer.is_active.is_(True))
        .values(**values)
    )


async def get_price_rub(session: AsyncSession) -> int:
    return int(await get_app_setting_int(session, "price_rub", default=settings.price_rub) or settings.price_rub)


async def is_trial_available(session: AsyncSession, tg_id: int) -> bool:
    used = int(await get_app_setting_int(session, f"trial_used:{int(tg_id)}", default=0) or 0)
    return used == 0


async def set_trial_used(session: AsyncSession, tg_id: int) -> None:
    await set_app_setting_int(session, f"trial_used:{int(tg_id)}", 1)


async def has_used_trial(session: AsyncSession, tg_id: int) -> bool:
    return not await is_trial_available(session, tg_id)


async def has_successful_payments(session: AsyncSession, tg_id: int) -> bool:
    row = await session.scalar(
        select(Payment.id)
        .where(Payment.tg_id == int(tg_id), Payment.status == "success")
        .order_by(Payment.id.desc())
        .limit(1)
    )
    return row is not None


async def get_active_peer(session: AsyncSession, tg_id: int) -> VpnPeer | None:
    return await session.scalar(
        select(VpnPeer)
        .where(VpnPeer.tg_id == int(tg_id), VpnPeer.is_active.is_(True))
        .order_by(VpnPeer.id.desc())
        .limit(1)
    )


async def create_content_request(
    session: AsyncSession,
    *,
    user_id: int,
    content_url: str,
    ttl_seconds: int | None = None,
) -> ContentRequest:
    now = utcnow()
    ttl = int(ttl_seconds or settings.content_request_ttl_seconds)
    row = ContentRequest(
        user_id=int(user_id),
        token=str(uuid4()),
        content_url=content_url,
        created_at=now,
        expires_at=now + timedelta(seconds=ttl),
    )
    session.add(row)
    await session.flush()
    return row


async def get_content_request_by_token(session: AsyncSession, token: str) -> ContentRequest | None:
    now = utcnow()
    return await session.scalar(
        select(ContentRequest)
        .where(ContentRequest.token == token, ContentRequest.expires_at > now)
        .limit(1)
    )
