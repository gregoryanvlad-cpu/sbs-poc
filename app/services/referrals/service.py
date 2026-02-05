from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import case, func, select

from app.core.config import settings
from app.db.models import Payment, Referral, ReferralEarning, User
from app.db.models.payout_request import PayoutRequest


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _level_percent(active_referrals: int) -> int:
    """3-level commission:
    1..3 -> 5%
    4..9 -> 11%
    10+  -> 17%
    """
    if active_referrals >= 10:
        return 17
    if active_referrals >= 4:
        return 11
    return 5


class ReferralService:
    # =====================================================
    # BASIC INFO / BALANCES
    # =====================================================

    async def count_active_referrals(self, session, tg_id: int) -> int:
        cnt = await session.scalar(
            select(func.count(Referral.id)).where(
                Referral.referrer_tg_id == int(tg_id),
                Referral.status == "active",
            )
        )
        return int(cnt or 0)

    async def current_percent(self, session, tg_id: int) -> int:
        return _level_percent(await self.count_active_referrals(session, tg_id))

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
        """Returns (available_sum, pending_sum, paid_sum) in RUB integers."""
        pending, available = await self.get_balance(session, tg_id)

        paid = await session.scalar(
            select(func.coalesce(func.sum(ReferralEarning.earned_rub), 0)).where(
                ReferralEarning.referrer_tg_id == int(tg_id),
                ReferralEarning.status == "paid",
            )
        )
        return int(available or 0), int(pending or 0), int(paid or 0)

    async def available_balance(self, session, *, tg_id: int) -> Decimal:
        _, avail = await self.get_balance(session, tg_id)
        return Decimal(int(avail))

    # =====================================================
    # WHO INVITED ME
    # =====================================================

    async def get_inviter_tg_id(self, session, *, tg_id: int) -> int | None:
        """Return inviter TG id if this user became an ACTIVE referral (after first payment)."""
        inviter = await session.scalar(
            select(Referral.referrer_tg_id).where(
                Referral.referred_tg_id == int(tg_id),
                Referral.status == "active",
            ).limit(1)
        )
        return int(inviter) if inviter is not None else None

    async def get_my_referrer_label(self, session, *, tg_id: int) -> str:
        inviter = await self.get_inviter_tg_id(session, tg_id=int(tg_id))
        return f"ID {inviter}" if inviter else "самостоятельно"

    # =====================================================
    # CABINET: REFERRALS LIST (matches nav.py expectations)
    # =====================================================

    async def list_referrals_summary(
        self,
        session,
        *,
        tg_id: int,
        limit: int = 50,
    ) -> list[dict]:
        """Return per-referral earnings breakdown.

        nav.py expects keys:
          total, available, pending, paid
        """
        q = (
            select(
                Referral.referred_tg_id.label("referred_tg_id"),
                Referral.status.label("ref_status"),
                Referral.activated_at.label("activated_at"),
                func.coalesce(func.sum(ReferralEarning.earned_rub), 0).label("total"),
                func.coalesce(
                    func.sum(
                        case(
                            (ReferralEarning.status == "available", ReferralEarning.earned_rub),
                            else_=0,
                        )
                    ),
                    0,
                ).label("available"),
                func.coalesce(
                    func.sum(
                        case(
                            (ReferralEarning.status == "pending", ReferralEarning.earned_rub),
                            else_=0,
                        )
                    ),
                    0,
                ).label("pending"),
                func.coalesce(
                    func.sum(
                        case(
                            (ReferralEarning.status == "paid", ReferralEarning.earned_rub),
                            else_=0,
                        )
                    ),
                    0,
                ).label("paid"),
                func.max(Referral.id).label("rid"),
            )
            .outerjoin(
                ReferralEarning,
                (ReferralEarning.referred_tg_id == Referral.referred_tg_id)
                & (ReferralEarning.referrer_tg_id == Referral.referrer_tg_id),
            )
            .where(Referral.referrer_tg_id == int(tg_id))
            .group_by(Referral.referred_tg_id, Referral.status, Referral.activated_at)
            .order_by(Referral.activated_at.desc().nullslast(), func.max(Referral.id).desc())
            .limit(int(limit))
        )

        rows = (await session.execute(q)).all()
        out: list[dict] = []
        for row in rows:
            m = row._mapping  # SQLAlchemy RowMapping
            out.append(
                {
                    "referred_tg_id": int(m["referred_tg_id"]),
                    "status": str(m["ref_status"] or "active"),
                    "activated_at": m["activated_at"],
                    "total": int(m["total"] or 0),
                    "available": int(m["available"] or 0),
                    "pending": int(m["pending"] or 0),
                    "paid": int(m["paid"] or 0),
                }
            )
        return out

    # =====================================================
    # REF CODE / CLICK
    # =====================================================

    async def ensure_ref_code(self, session, tg_id_or_user: Any) -> str:
        """Accepts either tg_id:int OR User instance."""
        tg_id = int(tg_id_or_user.tg_id if isinstance(tg_id_or_user, User) else tg_id_or_user)

        user = await session.get(User, tg_id)
        if not user:
            user = User(tg_id=tg_id)
            session.add(user)
            await session.flush()

        if user.ref_code:
            return user.ref_code

        for _ in range(10):
            code = secrets.token_urlsafe(8).rstrip("=")
            exists = await session.scalar(select(User.tg_id).where(User.ref_code == code).limit(1))
            if not exists:
                user.ref_code = code
                await session.flush()
                return code

        user.ref_code = f"u{tg_id}"
        await session.flush()
        return user.ref_code

    async def attach_pending_referrer(self, session, *, referred_tg_id: int, ref_code: str) -> None:
        """Save referral click (pending) if this user wasn't referred before."""
        if not ref_code:
            return

        referrer = await session.scalar(select(User).where(User.ref_code == ref_code).limit(1))
        if not referrer or int(referrer.tg_id) == int(referred_tg_id):
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

    # =====================================================
    # PAYMENT HOOK
    # =====================================================

    async def on_payment_success(self, session, *, payment: Payment) -> None:
        """Process referral activation + commission for this successful payment.

        Commission should be начислена на КАЖДЫЙ успешный платеж реферала (включая продления),
        но строго исключая самореферал (payer == referrer).
        """
        if not payment or payment.status != "success":
            return

        payer_id = int(payment.tg_id)

        # Determine referrer:
        # 1) Prefer explicit link on User (fast path)
        # 2) Fallback to existing active Referral row (resilient for older data)
        referrer_id: int | None = None

        user = await session.get(User, payer_id)
        if user and user.referred_by_tg_id:
            referrer_id = int(user.referred_by_tg_id)

        if not referrer_id:
            referral = await session.scalar(
                select(Referral)
                .where(
                    Referral.referred_tg_id == payer_id,
                    Referral.status == "active",
                )
                .order_by(Referral.id.desc())
                .limit(1)
            )
            if referral:
                referrer_id = int(referral.referrer_tg_id)

        # No referrer -> nothing to начислять
        if not referrer_id:
            return

        # Block self-referral (even if someone manually set it in DB for tests)
        if int(referrer_id) == int(payer_id):
            return

        # Create referral if first payment (activation)
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

        percent = _level_percent(await self.count_active_referrals(session, referrer_id))
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

        session.add(
            ReferralEarning(
                referrer_tg_id=referrer_id,
                referred_tg_id=payer_id,
                payment_id=payment.id,
                payment_amount_rub=pay_amount,
                percent=percent,
                earned_rub=earned,
                status="pending" if hold_days else "available",
                available_at=available_at if hold_days else None,
            )
        )
        await session.flush()

        percent = _level_percent(await self.count_active_referrals(session, referrer_id))
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

        session.add(
            ReferralEarning(
                referrer_tg_id=referrer_id,
                referred_tg_id=payer_id,
                payment_id=payment.id,
                payment_amount_rub=pay_amount,
                percent=percent,
                earned_rub=earned,
                status="pending" if hold_days else "available",
                available_at=available_at if hold_days else None,
            )
        )
        await session.flush()

    # Backward-compat: nav.py calls on_successful_payment(session, payment)
    async def on_successful_payment(self, session, payment: Payment) -> None:
        await self.on_payment_success(session, payment=payment)

    # =====================================================
    # RELEASE HOLD
    # =====================================================

    async def release_pending(self, session) -> int:
        now = _utcnow()
        items = (
            await session.scalars(
                select(ReferralEarning).where(
                    ReferralEarning.status == "pending",
                    ReferralEarning.available_at.is_not(None),
                    ReferralEarning.available_at <= now,
                )
            )
        ).all()

        for e in items:
            e.status = "available"
        await session.flush()
        return len(items)

    # =====================================================
    # PAYOUTS
    # =====================================================

    async def create_payout_request(
        self,
        session,
        *,
        tg_id: int,
        amount_rub: int,
        requisites: str,
    ) -> PayoutRequest:
        min_amt = int(getattr(settings, "referral_min_payout_rub", 50) or 50)
        if int(amount_rub) < min_amt:
            raise ValueError("amount_below_min")

        _, available = await self.get_balance(session, int(tg_id))
        if int(amount_rub) > int(available):
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

        items = (
            await session.scalars(
                select(ReferralEarning)
                .where(
                    ReferralEarning.referrer_tg_id == int(tg_id),
                    ReferralEarning.status == "available",
                )
                .order_by(ReferralEarning.id.asc())
            )
        ).all()

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
                session.add(
                    ReferralEarning(
                        referrer_tg_id=e.referrer_tg_id,
                        referred_tg_id=e.referred_tg_id,
                        payment_id=e.payment_id,
                        payment_amount_rub=int(e.payment_amount_rub or 0),
                        percent=int(e.percent or 0),
                        earned_rub=remaining,
                        status="reserved",
                        payout_request_id=req.id,
                    )
                )
                e.earned_rub = int(e.earned_rub or 0) - remaining
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

        items = (
            await session.scalars(
                select(ReferralEarning).where(
                    ReferralEarning.payout_request_id == int(request_id),
                    ReferralEarning.status == "reserved",
                )
            )
        ).all()

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

        items = (
            await session.scalars(
                select(ReferralEarning).where(
                    ReferralEarning.payout_request_id == int(request_id),
                    ReferralEarning.status == "reserved",
                )
            )
        ).all()

        for e in items:
            e.status = "available"
            e.payout_request_id = None
        await session.flush()


referral_service = ReferralService()
