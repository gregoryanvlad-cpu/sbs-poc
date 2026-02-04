from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Referral(Base):
    """Referral relationship.

    We create it on the referred user's FIRST successful payment.
    """

    __tablename__ = "referrals"
    __table_args__ = (UniqueConstraint("referred_tg_id", name="uq_referrals_referred"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    referrer_tg_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    referred_tg_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)

    status: Mapped[str] = mapped_column(String(16), server_default="active", nullable=False)

    first_payment_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
