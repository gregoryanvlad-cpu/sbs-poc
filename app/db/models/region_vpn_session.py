from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String

from app.db.base import Base


class RegionVpnSession(Base):
    """Tracks last active public IP per user for VPN-Region (VLESS+Reality).

    Used to implement "last connected device wins" without changing client config.
    """

    __tablename__ = "region_vpn_sessions"

    tg_id = Column(Integer, primary_key=True, index=True)
    active_ip = Column(String(64), nullable=True)  # public IP observed in Xray access.log
    last_seen_at = Column(DateTime(timezone=True), nullable=True)
    last_switch_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=True, default=datetime.utcnow)
