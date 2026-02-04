from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class User(Base):
    __tablename__ = "users"

    tg_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    flow_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    flow_data: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ==========================
    # Referrals
    # ==========================
    # Stable personal referral code used in /start payload.
    ref_code: Mapped[str | None] = mapped_column(String(32), unique=True, index=True, nullable=True)
    # Who invited this user (stored on /start, activated on first successful payment).
    referred_by_tg_id: Mapped[int | None] = mapped_column(BigInteger, index=True, nullable=True)
    referred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
