from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Payment, Subscription, User, VpnPeer

log = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def ensure_user(session: AsyncSession, tg_id: int) -> User:
    """
    Ensures User exists and also ensures Subscription exists.

    IMPORTANT:
    Subscription.tg_id has FK -> users.tg_id
    So we MUST flush User first, then create Subscription.
    """
    user = await session.get(User, tg_id)
    if not user:
        user = User(tg_id=tg_id)
        session.add(user)
        await session.flush()  # ✅ user row exists now

    sub = await session.get(Subscription, tg_id)
    if not sub:
        sub = Subscription(tg_id=tg_id)
        session.add(sub)
        await session.flush()

    return user


async def get_subscription(session: AsyncSession, tg_id: int) -> Subscription:
    sub = await session.get(Subscription, tg_id)
    if not sub:
        await ensure_user(session, tg_id)
        sub = await session.get(Subscription, tg_id)
        if not sub:
            sub = Subscription(tg_id=tg_id)
            session.add(sub)
            await session.flush()
    return sub


async def extend_subscription(session: AsyncSession, tg_id: int, *, months: int, days_legacy: int) -> Subscription:
    """
    Marks subscription active and writes Payment row.
    Caller is responsible for calculating and setting sub.end_at.
    """
    await ensure_user(session, tg_id)

    sub = await get_subscription(session, tg_id)
    sub.is_active = True
    sub.status = "active"
    if not sub.start_at:
        sub.start_at = utcnow()

    await session.flush()

    payment = Payment(
        tg_id=tg_id,
        amount=299,
        currency="RUB",
        provider="mock",
        status="success",
        period_days=days_legacy,
        period_months=months,
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
