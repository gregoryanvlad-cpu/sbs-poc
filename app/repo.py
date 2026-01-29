from __future__ import annotations

from datetime import datetime
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.yandex_account import YandexAccount


async def pick_account(session: AsyncSession, need_cover_until: datetime | None = None) -> YandexAccount | None:
    """
    Выбирает активный yandex_account с доступным слотом.
    need_cover_until: если задано — аккаунт должен иметь plus_end_at >= need_cover_until
    """

    q = select(YandexAccount).where(YandexAccount.status == "active")

    # учитываем покрытие (если поле есть в модели)
    if need_cover_until is not None:
        q = q.where(
            (YandexAccount.plus_end_at.is_(None)) | (YandexAccount.plus_end_at >= need_cover_until)
        )

    # свободные слоты: used_slots < (max_slots-1)
    q = q.where(YandexAccount.used_slots < (YandexAccount.max_slots - 1))

    # берём самый "свободный"
    q = q.order_by(YandexAccount.used_slots.asc(), YandexAccount.id.asc()).limit(1)

    res = await session.execute(q)
    return res.scalar_one_or_none()
