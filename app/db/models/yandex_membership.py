from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class YandexMembership(Base):
    __tablename__ = "yandex_memberships"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # user
    tg_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.tg_id", ondelete="CASCADE"))

    # yandex account / slot
    yandex_account_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("yandex_accounts.id"))
    invite_slot_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("yandex_invite_slots.id"))

    # manual helpers for admin/reporting
    account_label: Mapped[str | None] = mapped_column(String(64))
    slot_index: Mapped[int | None] = mapped_column(Integer)

    # legacy/login (оставляем, чтобы ничего не ломать)
    yandex_login: Mapped[str] = mapped_column(String(128), nullable=False)

    # state
    status: Mapped[str] = mapped_column(String(24), server_default="pending")

    # invite
    invite_link: Mapped[str | None] = mapped_column(String(512))
    invite_issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    invite_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # stats
    reinvite_used: Mapped[int] = mapped_column(Integer, server_default="0")
    abuse_strikes: Mapped[int] = mapped_column(Integer, server_default="0")

    # coverage freeze (ключевое для ротации)
    coverage_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    switch_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # ✅ УВЕДОМЛЕНИЯ (7/3/1) — отправляются 1 раз
    notified_7d_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notified_3d_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notified_1d_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # ✅ УЧЁТ ИСКЛЮЧЕНИЯ ИЗ СЕМЬИ (вручную админом)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
