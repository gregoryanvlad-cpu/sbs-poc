from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.repo import utcnow


class YandexMembership(Base):
    __tablename__ = "yandex_memberships"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Telegram user
    tg_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)

    # Связь с аккаунтом Яндекса
    yandex_account_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("yandex_accounts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Номер слота (1 / 2 / 3)
    slot_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Инвайт-ссылка (последняя выданная)
    invite_link: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # До какого момента действует покрытие этим аккаунтом
    coverage_end_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # --- УВЕДОМЛЕНИЯ ПОЛЬЗОВАТЕЛЮ ---
    # (чтобы не слать повторно)

    notified_7d_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    notified_3d_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    notified_1d_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # --- УЧЁТ ИСКЛЮЧЕНИЯ ИЗ СЕМЬИ ---
    removed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Технические поля
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
    )

    # Relationships
    yandex_account = relationship("YandexAccount", back_populates="memberships")

    # -----------------------------
    # Удобные computed helpers
    # -----------------------------

    def is_removed(self) -> bool:
        return self.removed_at is not None

    def mark_removed(self) -> None:
        self.removed_at = utcnow()

    def mark_notified_7d(self) -> None:
        self.notified_7d_at = utcnow()

    def mark_notified_3d(self) -> None:
        self.notified_3d_at = utcnow()

    def mark_notified_1d(self) -> None:
        self.notified_1d_at = utcnow()
