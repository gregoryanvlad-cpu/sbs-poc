from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class YandexInviteSlot(Base):
    """Manual (one-time) invite links pool.

    Strategy S1: each slot/link is issued at most once and never reused.
    """

    __tablename__ = "yandex_invite_slots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    yandex_account_id: Mapped[int] = mapped_column(Integer, ForeignKey("yandex_accounts.id", ondelete="CASCADE"))
    slot_index: Mapped[int] = mapped_column(Integer, nullable=False)  # 1..3

    invite_link: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(16), server_default="free", nullable=False)  # free/issued/burned

    issued_to_tg_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
