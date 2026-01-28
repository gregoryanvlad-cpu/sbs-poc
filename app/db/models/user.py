from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


\1    flow_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    flow_data: Mapped[str | None] = mapped_column(String(), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    status: Mapped[str] = mapped_column(String(16), server_default="active", nullable=False)
