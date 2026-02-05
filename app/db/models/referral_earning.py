from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ReferralEarning(Base):
    """Commission line for a single payment made by a referred user."""

    __tablename__ = "referral_earnings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    referrer_tg_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    referred_tg_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    # For automated earnings this points to the related `payments.id`.
    # For manual/admin mint operations there may be no underlying payment,
    # so the column must allow NULL.
    payment_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)

    # payment data snapshot
    payment_amount_rub: Mapped[int] = mapped_column(Integer, nullable=False)
    percent: Mapped[int] = mapped_column(Integer, nullable=False)
    earned_rub: Mapped[int] = mapped_column(Integer, nullable=False)

    # pending -> available -> paid
    status: Mapped[str] = mapped_column(String(16), server_default="pending", nullable=False)
    available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    payout_request_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
