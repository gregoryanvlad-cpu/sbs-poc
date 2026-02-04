from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class PayoutRequest(Base):
    """User withdraw request.

    Created by user in the bot. Processed manually by owner.
    """

    __tablename__ = "payout_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    tg_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    amount_rub: Mapped[int] = mapped_column(Integer, nullable=False)

    # created -> approved -> paid | rejected
    status: Mapped[str] = mapped_column(String(16), server_default="created", nullable=False)

    requisites: Mapped[str | None] = mapped_column(Text, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
