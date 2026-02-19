from __future__ import annotations


# ---- Runtime settings (admin-tunable) ----------------------------------------
def _app_setting_model():
    """Import AppSetting lazily to avoid startup failures if migrations are not applied yet."""
    try:
        from app.db.models.app_setting import AppSetting  # type: ignore
        return AppSetting
    except Exception:
        return None

import logging
from datetime import datetime, timezone, timedelta
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Payment, Subscription, User, VpnPeer, ContentRequest, AppSetting
from app.core.config import settings

log = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def get_app_setting_int(session: AsyncSession, key: str, *, default: int) -> int:
    row = await session.get(AppSetting, key)
    if not row or row.int_value is None:
        return int(default)
    return int(row.int_value)


async def set_app_setting_int(session: AsyncSession, key: str, value: int) -> None:
    row = await session.get(AppSetting, key)
    if not row:
        row = AppSetting(key=key)
        session.add(row)
    row.int_value = int(value)
    row.updated_at = utcnow()
    await session.flush()


async def get_price_rub(session: AsyncSession) -> int:
    """Runtime-tunable price. Falls back to static settings.price_rub."""
    return await get_app_setting_int(session, "price_rub", default=settings.price_rub)


async def create_content_request(
    session: AsyncSession,
    tg_id: int,
    *,
    content_url: str,
    ttl_seconds: int = 900,
) -> str:
    """Create a short-lived token for Bot2 deep-link.

    Returns:
        token (uuid4 string)
    """
    token = str(uuid4())
    now = utcnow()
    expires_at = now + timedelta(seconds=max(60, int(ttl_seconds)))
    session.add(
        ContentRequest(
            user_id=tg_id,
            token=token,
            content_url=content_url,
            created_at=now,
            expires_at=expires_at,
        )
    )
    await session.flush()
    return token


async def get_content_request_by_token(session: AsyncSession, token: str) -> ContentRequest | None:
    now = utcnow()
    q = select(ContentRequest).where(ContentRequest.token == token, ContentRequest.expires_at > now).limit(1)
    res = await session.execute(q)
    return res.scalar_one_or_none()


async def ensure_user(
    session: AsyncSession,
    tg_id: int,
    *,
    username: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
) -> User:
    """
    Ensures User row exists and also ensures an empty Subscription row exists.

    IMPORTANT:
    We must flush User first, because Subscription.tg_id has FK -> users.tg_id.
    Otherwise Postgres may throw: subscriptions_tg_id_fkey.
    """
    user = await session.get(User, tg_id)
    if not user:
        user = User(
            tg_id=tg_id,
            tg_username=(username or None),
            first_name=(first_name or None),
            last_name=(last_name or None),
        )
        session.add(user)
        await session.flush()  # ✅ user must exist before subscription
    else:
        # Best-effort: keep Telegram profile snapshot somewhat fresh.
        changed = False
        if username is not None and user.tg_username != username:
            user.tg_username = username
            changed = True
        if first_name is not None and user.first_name != first_name:
            user.first_name = first_name
            changed = True
        if last_name is not None and user.last_name != last_name:
            user.last_name = last_name
            changed = True
        if changed:
            user.updated_at = utcnow()
            await session.flush()

    sub = await session.get(Subscription, tg_id)
    if not sub:
        sub = Subscription(tg_id=tg_id)
        session.add(sub)
        await session.flush()

    # Ensure the user has a referral code
    try:
        from app.services.referrals.service import referral_service

        await referral_service.ensure_ref_code(session, tg_id)
    except Exception:
        # best-effort
        pass

    return user


async def get_subscription(session: AsyncSession, tg_id: int) -> Subscription:
    sub = await session.get(Subscription, tg_id)
    if not sub:
        # Defensive: create subscription only after ensuring user exists
        await ensure_user(session, tg_id)
        sub = await session.get(Subscription, tg_id)
        if not sub:
            # should not happen, but keep safe
            sub = Subscription(tg_id=tg_id)
            session.add(sub)
            await session.flush()
    return sub


async def extend_subscription(
    session: AsyncSession,
    tg_id: int,
    *,
    months: int,
    days_legacy: int,
    amount_rub: int = 199,
    provider: str = "mock",
    status: str = "success",
    provider_payment_id: str | None = None,
) -> Subscription:
    """Extends subscription end_at by calendar months.

    Caller is responsible for computing end_at and setting sub.end_at.
    """
    # Ensure user+subscription exist
    await ensure_user(session, tg_id)

    sub = await get_subscription(session, tg_id)
    sub.is_active = True
    sub.status = "active"

    if not sub.start_at:
        sub.start_at = utcnow()

    await session.flush()

    # also insert/update payment row for history (idempotent by provider_payment_id)
    if provider_payment_id:
        q = select(Payment).where(Payment.provider_payment_id == provider_payment_id).limit(1)
        res = await session.execute(q)
        existing = res.scalar_one_or_none()
    else:
        existing = None

    if existing:
        # Update the existing row (e.g. pending -> success) instead of inserting a duplicate
        existing.amount = int(amount_rub)
        existing.currency = "RUB"
        existing.provider = provider
        existing.status = status
        existing.period_days = int(days_legacy)
        existing.period_months = int(months)
        if status == "success":
            existing.paid_at = utcnow()
        await session.flush()
        return sub

    payment = Payment(
        tg_id=tg_id,
        amount=int(amount_rub),
        currency="RUB",
        provider=provider,
        status=status,
        period_days=days_legacy,
        period_months=months,
        provider_payment_id=provider_payment_id,
    )
    session.add(payment)
    await session.flush()
    return sub


async def get_active_peer(session: AsyncSession, tg_id: int) -> VpnPeer | None:
    q = (
        select(VpnPeer)
        .where(VpnPeer.tg_id == tg_id, VpnPeer.is_active == True)
        .order_by(VpnPeer.id.desc())
        .limit(1)
    )
    res = await session.execute(q)
    return res.scalar_one_or_none()


async def deactivate_peers(session: AsyncSession, tg_id: int, *, reason: str | None = None) -> None:
    stmt = (
        update(VpnPeer)
        .where(VpnPeer.tg_id == tg_id, VpnPeer.is_active == True)
        .values(is_active=False, revoked_at=utcnow(), rotation_reason=reason)
    )
    await session.execute(stmt)


async def list_expired_subscriptions(session: AsyncSession, now: datetime) -> list[Subscription]:
    q = select(Subscription).where(
        Subscription.is_active == True,
        Subscription.end_at.is_not(None),
        Subscription.end_at <= now,
    )
    res = await session.execute(q)
    return list(res.scalars().all())


async def set_subscription_expired(session: AsyncSession, tg_id: int) -> None:
    sub = await get_subscription(session, tg_id)
    sub.is_active = False
    sub.status = "expired"
    await session.flush()

async def get_setting_int(session, key: str) -> int | None:
    AppSetting = _app_setting_model()
    if AppSetting is None:
        return None
    obj = await session.get(AppSetting, key)
    return None if obj is None else obj.int_value


async def set_setting_int(session, key: str, value: int | None) -> None:
    AppSetting = _app_setting_model()
    if AppSetting is None:
        return
    obj = await session.get(AppSetting, key)
    if obj is None:
        obj = AppSetting(key=key, int_value=value)
        session.add(obj)
    else:
        obj.int_value = value
    try:
        obj.touch()
    except Exception:
        pass
