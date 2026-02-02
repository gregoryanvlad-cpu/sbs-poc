from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Tuple

from dateutil.relativedelta import relativedelta
from sqlalchemy import and_, select

from app.db.models.subscription import Subscription
from app.db.models.yandex_account import YandexAccount
from app.db.models.yandex_invite_slot import YandexInviteSlot
from app.db.models.yandex_membership import YandexMembership
from app.repo import utcnow


@dataclass
class ManualInviteIssueResult:
    membership: YandexMembership
    invite_link: str


def _ensure_tz(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _plus_has_one_month_left(acc: YandexAccount, *, now: datetime) -> bool:
    """Manual rule: allow issuing to this account only if it will live >= 1 calendar month.

    plus_end_at is entered manually in admin.
    """
    if not acc.plus_end_at:
        return False
    end_at = _ensure_tz(acc.plus_end_at)
    return end_at >= (now + relativedelta(months=1))


async def _pick_slot_for_issue(session, *, now: datetime) -> Tuple[YandexAccount, YandexInviteSlot]:
    """Pick an eligible account + first free slot.

    Strategy S1:
    - slot/link is used at most once
    - we never re-use freed slots
    """
    # eligible accounts: active, with enough lifetime
    accounts = (
        await session.scalars(
            select(YandexAccount).where(YandexAccount.status == "active").order_by(YandexAccount.id.asc())
        )
    ).all()
    if not accounts:
        raise RuntimeError("No active YandexAccount")

    eligible_ids = [a.id for a in accounts if _plus_has_one_month_left(a, now=now)]
    if not eligible_ids:
        raise RuntimeError("No YandexAccount with enough Plus lifetime")

    # pick first free slot in the earliest eligible account (stable ordering)
    q = (
        select(YandexInviteSlot)
        .where(
            and_(
                YandexInviteSlot.yandex_account_id.in_(eligible_ids),
                YandexInviteSlot.status == "free",
            )
        )
        .order_by(YandexInviteSlot.yandex_account_id.asc(), YandexInviteSlot.slot_index.asc(), YandexInviteSlot.id.asc())
        .limit(1)
    )
    slot = await session.scalar(q)
    if not slot:
        raise RuntimeError("No free invite slots")

    acc = await session.get(YandexAccount, slot.yandex_account_id)
    if not acc:
        raise RuntimeError("YandexAccount not found for slot")
    return acc, slot


class YandexService:
    """Manual Yandex invites (no Playwright).

    Public contract is intentionally compatible with existing handlers:
    - ensure_membership_for_user(...)

    Everything else (Playwright, TTL, family scans) is no longer used.
    """

    async def ensure_membership_for_user(
        self,
        *,
        session,
        tg_id: int,
        yandex_login: str,
    ) -> YandexMembership:
        """Return existing membership with invite_link, or issue a new one from the pool."""
        now = utcnow()

        # latest membership for user
        existing = await session.scalar(
            select(YandexMembership)
            .where(YandexMembership.tg_id == tg_id)
            .order_by(YandexMembership.id.desc())
            .limit(1)
        )
        if existing and existing.invite_link:
            return existing

        # subscription must be active
        sub = await session.scalar(select(Subscription).where(Subscription.tg_id == tg_id).limit(1))
        if not sub or not sub.end_at or _ensure_tz(sub.end_at) <= now:
            raise RuntimeError("Subscription is not active")

        acc, slot = await _pick_slot_for_issue(session, now=now)

        # mark slot as issued (and thus burned)
        slot.status = "issued"
        slot.issued_to_tg_id = tg_id
        slot.issued_at = now

        membership = YandexMembership(
            tg_id=tg_id,
            yandex_account_id=acc.id,
            invite_slot_id=slot.id,
            account_label=acc.label,
            slot_index=int(slot.slot_index),
            yandex_login=(yandex_login or "").strip().lstrip("@").lower(),
            status="issued",
            invite_link=slot.invite_link,
            invite_issued_at=now,
            coverage_end_at=_ensure_tz(sub.end_at),  # freeze coverage for this issued slot
        )
        session.add(membership)
        await session.flush()
        return membership

    async def rotate_due_memberships(self, session) -> List[Tuple[int, str]]:
        """Issue a new invite for users whose frozen coverage ended but subscription is still active.

        Returns list of (tg_id, invite_link) to notify.
        """
        now = utcnow()
        q = (
            select(YandexMembership, Subscription)
            .join(Subscription, Subscription.tg_id == YandexMembership.tg_id)
            .where(
                YandexMembership.coverage_end_at.is_not(None),
                YandexMembership.coverage_end_at <= now,
                Subscription.end_at.is_not(None),
                Subscription.end_at > now,
            )
            .order_by(YandexMembership.id.asc())
            .limit(50)
        )
        rows = (await session.execute(q)).all()
        if not rows:
            return []

        notifications: List[Tuple[int, str]] = []
        for membership, sub in rows:
            # issue new slot, keep yandex_login as-is
            acc, slot = await _pick_slot_for_issue(session, now=now)
            slot.status = "issued"
            slot.issued_to_tg_id = membership.tg_id
            slot.issued_at = now

            membership.yandex_account_id = acc.id
            membership.invite_slot_id = slot.id
            membership.account_label = acc.label
            membership.slot_index = int(slot.slot_index)
            membership.invite_link = slot.invite_link
            membership.invite_issued_at = now
            membership.coverage_end_at = _ensure_tz(sub.end_at)  # re-freeze to the new paid end
            membership.status = "issued"
            membership.updated_at = now

            notifications.append((membership.tg_id, slot.invite_link))

        await session.flush()
        return notifications


yandex_service = YandexService()
