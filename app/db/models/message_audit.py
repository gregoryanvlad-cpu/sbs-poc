from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, Text, func, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class MessageAudit(Base):
    """Best-effort outgoing message log.

    Telegram does not provide a reliable "read"/"seen" signal for bot messages.
    We approximate "read" as: the user interacted with the bot (message/callback)
    after the message was sent.
    """

    __tablename__ = "message_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    tg_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)

    # Where it came from (scheduler, admin broadcast, etc.)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)

    # Telegram message id (if send succeeded)
    chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Human-friendly preview (kept short)
    text_preview: Mapped[str] = mapped_column(Text, nullable=False)

    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # Set when the user interacts with the bot after sent_at (approx "read")
    seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


Index("ix_message_audit_tg_id_sent_at", MessageAudit.tg_id, MessageAudit.sent_at)
