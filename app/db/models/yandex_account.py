
from datetime import datetime
from sqlalchemy import Integer, String, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base

class YandexAccount(Base):
    __tablename__ = "yandex_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(16), server_default="active", nullable=False)
    max_slots: Mapped[int] = mapped_column(Integer, server_default="4", nullable=False)
    used_slots: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    plus_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    credentials_ref: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
