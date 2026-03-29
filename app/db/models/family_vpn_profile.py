from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class FamilyVpnProfile(Base):
    """A single VPN profile (WireGuard peer) purchased as part of owner's family group."""

    __tablename__ = "family_vpn_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_tg_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    # 1..10 slot number for UI
    slot_no: Mapped[int] = mapped_column(Integer, nullable=False)

    label: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # associated WireGuard peer (vpn_peers.id). Nullable until created.
    vpn_peer_id: Mapped[int | None] = mapped_column(ForeignKey("vpn_peers.id"), nullable=True)

    # per-seat paid coverage end (independent for each family place)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    is_paused: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
