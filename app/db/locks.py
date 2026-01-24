from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

SCHEDULER_LOCK_KEY = 947_382_611  # arbitrary stable int


async def try_advisory_lock(session: AsyncSession) -> bool:
    res = await session.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": SCHEDULER_LOCK_KEY})
    return bool(res.scalar())


async def advisory_unlock(session: AsyncSession) -> None:
    await session.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": SCHEDULER_LOCK_KEY})
