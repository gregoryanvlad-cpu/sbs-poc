"""
Project entrypoint.

Bot + scheduler can run in one process (default), or separately.

- BOT service: SCHEDULER_ENABLED=0
- WORKER service: SCHEDULER_ENABLED=1
"""

import asyncio
import contextlib

from sqlalchemy import text

from app.bot.app import run_bot
from app.core.config import settings
from app.core.logging import setup_logging
from app.db.migrations.repair import main as repair_schema
from app.db.session import init_engine, async_session
from app.scheduler.worker import run_scheduler


async def ensure_referral_schema() -> None:
    """
    Railway-safe schema repair.
    Ensures referral columns exist even if Alembic didn't run.
    """
    async with async_session() as session:
        # referrals.status
        await session.execute(text("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_name = 'referrals'
                  AND column_name = 'status'
            ) THEN
                ALTER TABLE referrals
                ADD COLUMN status VARCHAR NOT NULL DEFAULT 'pending';
            END IF;
        END$$;
        """))

        # referral_earnings.payment_amount_rub
        await session.execute(text("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_name = 'referral_earnings'
                  AND column_name = 'payment_amount_rub'
            ) THEN
                ALTER TABLE referral_earnings
                ADD COLUMN payment_amount_rub INTEGER NOT NULL DEFAULT 0;
            END IF;
        END$$;
        """))

        await session.commit()


async def main() -> None:
    setup_logging()

    # Init DB engine
    init_engine(settings.database_url)

    # Alembic safety net (idempotent)
    repair_schema()

    # 🔥 Critical: fix schema BEFORE bot/scheduler
    await ensure_referral_schema()

    scheduler_task = None
    if settings.scheduler_enabled:
        scheduler_task = asyncio.create_task(
            run_scheduler(),
            name="scheduler",
        )

    try:
        await run_bot()
    finally:
        if scheduler_task:
            scheduler_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await scheduler_task


if __name__ == "__main__":
    asyncio.run(main())
