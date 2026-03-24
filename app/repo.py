from __future__ import annotations

from datetime import datetime
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.yandex_account import YandexAccount
from app.db.models.yandex_invite_slot import YandexInviteSlot


async def pick_account(session: AsyncSession, need_cover_until: datetime | None = None) -> YandexAccount | None:
    """Return the earliest active Yandex account that still has at least one free invite slot.

    Legacy note: availability is determined by ``yandex_invite_slots``.
    ``used_slots`` is no longer considered a source of truth.
    """

    q = select(YandexAccount).where(YandexAccount.status == "active")

    if need_cover_until is not None:
        q = q.where(
            (YandexAccount.plus_end_at.is_(None)) | (YandexAccount.plus_end_at >= need_cover_until)
        )

    q = q.order_by(YandexAccount.id.asc())
    rows = (await session.execute(q)).scalars().all()
    if not rows:
        return None

    ids = [int(acc.id) for acc in rows]
    free_slot_account_ids = {
        int(x)
        for x in (
            await session.scalars(
                select(YandexInviteSlot.yandex_account_id).where(
                    and_(
                        YandexInviteSlot.yandex_account_id.in_(ids),
                        YandexInviteSlot.status == "free",
                    )
                )
            )
        ).all()
    }
    for acc in rows:
        if int(acc.id) in free_slot_account_ids:
            return acc
    return None
