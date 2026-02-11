from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ContentRequest(Base):
    """Temporary deep-link tokens (Bot1 -> Bot2).

    NOTE:
    The DB table is created either via Alembic or manually in Railway.
    We keep the model in code so we can insert/select rows.
    """

    __tablename__ = "content_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    token: Mapped[str] = mapped_column(String(36), nullable=False, unique=True, index=True)
    content_url: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
