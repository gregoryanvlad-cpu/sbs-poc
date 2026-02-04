from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select

from app.core.config import settings
from app.db.models import Payment, Referral, ReferralEarning, User
from app.db.models.payout_request import PayoutRequest


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _level_percent(active_referrals: int) -> int:
    """3-level commission:
    1..3 -> 5%, 4..9 -> 11%, 10+ -> 17%
    """
    if active_referrals >= 10:
        return 17
    if active_referrals >= 4:
        return 11
    # по ТЗ даже при 0 пусть будет 5 (чтобы UI не пугал)
    return 5


class ReferralService:
    # -------------------------
    # helpers / public API used by bot UI
    # -------------------------

    async def count_active_referrals(self, session, tg_id: int) -> int:
        cnt = await session.scalar(
            select(func.count(Referral.id)).where(
                Referral.referrer_tg_id == int(tg_id),
                Referral.status == "active",
            )
        )
        return int(cnt or 0)

    async def current_percent(self, session, tg_id: int) -> int:
        active_cnt = await self.count_active_referrals(session, tg_id)
        return _level_percent(active_cnt)

    async def get_balance(self, session, tg_id: int) -> tuple[int, int]:
        """Returns (pending_sum, available_sum) in RUB integers."""
        pending = await session.scalar(
            select(func.coalesce(func.sum(ReferralEarning.earned_rub), 0)).where(
                ReferralEarning.referrer_tg_id == int(tg_id),
                ReferralEarning.status == "pending",
            )
        )
        available = await session.scalar(
            select(func.coalesce(func.sum(ReferralEarning.earned_rub), 0)).where(
                ReferralEarning.referrer_tg_id == int(tg_id),
                ReferralEarning.status == "available",
            )
        )
        return int(pending or 0), int(available or 0)

    async def get_balances(self, session, tg_id: int) -> tuple[int, int, int]:
        """Returns (available_sum, pending_sum, paid_sum) in RUB integers.

        Used by the cabinet UI to show:
        - available: can be withdrawn
        - pending: hold/antifraud period
        - paid: already paid out
        """
        pending, available = await self.get_balance(session, tg_id)

        paid = await session.scalar(
            select(func.coalesce(func.sum(ReferralEarning.earned_rub), 0)).where(
                ReferralEarning.referrer_tg_id == int(tg_id),
                ReferralEarning.status == "paid",
            )
        )

        return int(available or 0), int(pending or 0), int(paid or 0)

    async def available_balance(self, session, *, tg_id: int) -> Decimal:
        """Used by withdrawals flow (returns Decimal for comparisons)."""
        _, avail = await self.get_balance(session, tg_id)
        return Decimal(int(avail))

    async def get_inviter_tg_id(self, session, *, tg_id: int) -> int | None:
        """
        Returns inviter TG id for a given user (who invited this user).
        If nobody invited (came organically) -> None.

        Important: since you count referrals only after FIRST payment,
        we consider "inviter" to be the active Referral row, not just click.
        """
        inviter = await session.scalar(
            select(Referral.referrer_tg_id).where(
                Referral.referred_tg_id == int(tg_id),
                Referral.status == "active",
            ).limit(1)
        )
        return int(inviter) if inviter is not None else None

    # -------------------------
    # code creation / click attach
    # -------------------------

    async def ensure_ref_code(self, session, tg_id_or_user: Any) -> str:
        """
        Accepts either tg_id:int OR User instance (because nav.py may pass user object).
        """
        if isinstance(tg_id_or_user, User):
            tg_id = int(tg_id_or_user.tg_id)
        else:
            tg_id = int(tg_id_or_user)

        user = await session.get(User, tg_id)
        if not user:
            user = User(tg_id=tg_id)
            session.add(user)
            await session.flush()

        if user.ref_code:
            return user.ref_code

        # Generate unique short code (10-12 chars url-safe)
        for _ in range(10):
            code = secrets.token_urlsafe(8).rstrip("=")
            exists = await session.scalar(select(User.tg_id).where(User.ref_code == code).limit(1))
            if not exists:
                user.ref_code = code
                await session.flush()
                return code

        # fallback
        code = f"u{tg_id}"
        user.ref_code = code
        await session.flush()
        return code

    async def attach_pending_referrer(self, session, *, referred_tg_id: int, ref_code: str) -> None:
        """Save referral click (pending) if this user wasn't referred before."""
        if not ref_code:
            return
        referrer = await session.scalar(select(User).where(User.ref_code == ref_code).limit(1))
        if not referrer:
            return
        if int(referrer.tg_id) == int(referred_tg_id):
            return

        user = await session.get(User, int(referred_tg_id))
        if not user:
            user = User(tg_id=int(referred_tg_id))
            session.add(user)
            await session.flush()

        # do not overwrite
        if user.referred_by_tg_id:
            return

        user.referred_by_tg_id = int(referrer.tg_id)
        user.referred_at = _utcnow()
        await session.flush()

    # -------------------------
    # payment hook
    # -------------------------

    async def on_payment_success(self, session, *, payment: Payment) -> None:
        """Process referral activation + commission for this successful payment."""
        if not payment or payment.status != "success":
            return

        payer_id = int(payment.tg_id)
        user = await session.get(User, payer_id)
        if not user or not user.referred_by_tg_id:
            return

        referrer_id = int(user.referred_by_tg_id)

        # Create referral if first payment.
        referral = await session.scalar(select(Referral).where(Referral.referred_tg_id == payer_id).limit(1))
        if not referral:
            referral = Referral(
                referrer_tg_id=referrer_id,
                referred_tg_id=payer_id,
                status="active",
                first_payment_id=payment.id,
                activated_at=payment.paid_at or _utcnow(),
            )
            session.add(referral)
            await session.flush()

        # Determine current level based on active referrals count.
        active_cnt = await self.count_active_referrals(session, referrer_id)
        percent = _level_percent(active_cnt)

        pay_amount = int(payment.amount or 0)
        earned = int(round(pay_amount * percent / 100.0))

        # Idempotency: one earning per (payment_id, referrer)
        exists = await session.scalar(
            select(ReferralEarning.id).where(
                ReferralEarning.payment_id == payment.id,
                ReferralEarning.referrer_tg_id == referrer_id,
            ).limit(1)
        )
        if exists:
            return

        hold_days = int(getattr(settings, "referral_hold_days", 7) or 7)
        available_at = (payment.paid_at or _utcnow()) + timedelta(days=hold_days)

        e = ReferralEarning(
            referrer_tg_id=referrer_id,
            referred_tg_id=payer_id,
            payment_id=payment.id,
            payment_amount_rub=pay_amount,
            percent=percent,
            earned_rub=earned,
            status="pending" if hold_days > 0 else "available",
            available_at=available_at if hold_days > 0 else None,
        )
        session.add(e)
        await session.flush()

    async def release_pending(self, session) -> int:
        """Move pending earnings to available when hold period passed."""
        now = _utcnow()
        q = select(ReferralEarning).where(
            ReferralEarning.status == "pending",
            ReferralEarning.available_at.is_not(None),
            ReferralEarning.available_at <= now,
        )
        items = (await session.scalars(q)).all()
        for e in items:
            e.status = "available"
        await session.flush()
        return len(items)

    # -------------------------
    # payout
    # -------------------------

    async def create_payout_request(
        self,
        session,
        *,
        tg_id: int,
        amount_rub: int,
        requisites: str,
    ) -> PayoutRequest:
        """Create withdraw request and reserve available earnings."""
        min_amt = int(getattr(settings, "referral_min_payout_rub", 50) or 50)
        if int(amount_rub) < min_amt:
            raise ValueError("amount_below_min")

        _, available_sum = await self.get_balance(session, int(tg_id))
        if int(amount_rub) > int(available_sum):
            raise ValueError("amount_exceeds_balance")

        req = PayoutRequest(
            tg_id=int(tg_id),
            amount_rub=int(amount_rub),
            requisites=(requisites or "").strip(),
            status="created",
        )
        session.add(req)
        await session.flush()  # get id

        remaining = int(amount_rub)

        q = (
            select(ReferralEarning)
            .where(
                ReferralEarning.referrer_tg_id == int(tg_id),
                ReferralEarning.status == "available",
            )
            .order_by(ReferralEarning.id.asc())
        )
        items = (await session.scalars(q)).all()

        for e in items:
            if remaining <= 0:
                break

            e_amt = int(e.earned_rub or 0)

            if e_amt <= remaining:
                e.status = "reserved"
                e.payout_request_id = req.id
                remaining -= e_amt
            else:
                # Split line
                reserved_part = ReferralEarning(
                    referrer_tg_id=e.referrer_tg_id,
                    referred_tg_id=e.referred_tg_id,
                    payment_id=e.payment_id,
                    payment_amount_rub=int(e.payment_amount_rub or 0),
                    percent=int(e.percent or 0),
                    earned_rub=remaining,
                    status="reserved",
                    payout_request_id=req.id,
                )
                e.earned_rub = int(e.earned_rub or 0) - remaining
                session.add(reserved_part)
                remaining = 0
                break

        if remaining != 0:
            raise RuntimeError("reserve_failed")

        await session.flush()
        return req

    async def mark_payout_paid(self, session, *, request_id: int) -> None:
        req = await session.get(PayoutRequest, int(request_id))
        if not req:
            raise ValueError("not_found")
        req.status = "paid"
        req.processed_at = _utcnow()

        q = select(ReferralEarning).where(
            ReferralEarning.payout_request_id == int(request_id),
            ReferralEarning.status == "reserved",
        )
        items = (await session.scalars(q)).all()
        for e in items:
            e.status = "paid"
            e.paid_at = req.processed_at
        await session.flush()

    async def reject_payout(self, session, *, request_id: int, note: str | None = None) -> None:
        req = await session.get(PayoutRequest, int(request_id))
        if not req:
            raise ValueError("not_found")
        req.status = "rejected"
        req.note = (note or "").strip() or None
        req.processed_at = _utcnow()

        q = select(ReferralEarning).where(
            ReferralEarning.payout_request_id == int(request_id),
            ReferralEarning.status == "reserved",
        )
        items = (await session.scalars(q)).all()
        for e in items:
            e.status = "available"
            e.payout_request_id = None
        await session.flush()


referral_service = ReferralService()
