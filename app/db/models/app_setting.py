from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String
from app.db.base import Base


class AppSetting(Base):
    """Small KV storage for runtime-tunable settings."""

    __tablename__ = "app_settings"

    key = Column(String(128), primary_key=True)
    int_value = Column(Integer, nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=True)

    def touch(self) -> None:
        self.updated_at = datetime.utcnow()
