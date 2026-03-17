from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class FamilyVpnGroup(Base):
    """Paid "family seats" for VPN, owned by a single Telegram user.

    This is NOT a Telegram group of users; it's a set of paid VPN profiles
    (WireGuard peers) that the owner can share with relatives/colleagues.
    """

    __tablename__ = "family_vpn_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_tg_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True, index=True)

    # how many seats were purchased (max 10)
    seats_total: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    # paid coverage end for the family group itself (separate from owner's Subscription)
    active_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # whether owner agreed to receive monthly invoice/reminder automatically
    billing_opt_in: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
