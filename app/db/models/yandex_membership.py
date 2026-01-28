
from datetime import datetime
from sqlalchemy import BigInteger, Integer, String, DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base

class YandexMembership(Base):
    __tablename__ = "yandex_memberships"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.tg_id", ondelete="CASCADE"))
    yandex_account_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("yandex_accounts.id"))
    yandex_login: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(24), server_default="pending")
    invite_link: Mapped[str | None] = mapped_column(String(512))
    invite_issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    invite_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reinvite_used: Mapped[int] = mapped_column(Integer, server_default="0")
    coverage_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    switch_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    abuse_strikes: Mapped[int] = mapped_column(Integer, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
