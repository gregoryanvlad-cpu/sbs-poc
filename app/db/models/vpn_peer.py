from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class VpnPeer(Base):
    __tablename__ = "vpn_peers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    client_public_key: Mapped[str] = mapped_column(String(128), nullable=False)
    client_private_key_enc: Mapped[str] = mapped_column(String, nullable=False)
    client_ip: Mapped[str] = mapped_column(String(64), nullable=False)
    # Optional server code for multi-location setups (e.g., NL/DE/TR/US).
    # Null means "legacy/default" (single-server deployments).
    server_code: Mapped[str | None] = mapped_column(String(8), index=True, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rotation_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
