from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), server_default="RUB", nullable=False)
    provider: Mapped[str] = mapped_column(String(32), server_default="mock", nullable=False)
    status: Mapped[str] = mapped_column(String(16), server_default="success", nullable=False)
    paid_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    period_days: Mapped[int] = mapped_column(Integer, server_default="30", nullable=False)
    period_months: Mapped[int] = mapped_column(Integer, server_default="1", nullable=False)

    # future idempotency
    provider_payment_id: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True)
