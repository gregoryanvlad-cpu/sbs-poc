
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.yandex_account import YandexAccount


def usable_slots(acc: YandexAccount) -> int:
    max_slots = acc.max_slots or 4
    return max(0, max_slots - 1)  # -1 admin


async def pick_account(session: AsyncSession, *, need_cover_until: datetime) -> YandexAccount | None:
    q = (
        select(YandexAccount)
        .where(
            YandexAccount.status == "active",
            YandexAccount.plus_end_at.is_not(None),
            YandexAccount.plus_end_at >= need_cover_until,
            YandexAccount.used_slots < (YandexAccount.max_slots - 1),
        )
        .order_by(YandexAccount.used_slots.asc(), YandexAccount.plus_end_at.asc())
        .with_for_update()
    )
    res = await session.execute(q)
    return res.scalar_one_or_none()
