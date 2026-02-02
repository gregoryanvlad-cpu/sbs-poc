"""Project entrypoint.

Bot + scheduler can run in one process (default), or separately.

- BOT service: SCHEDULER_ENABLED=0
- WORKER service: SCHEDULER_ENABLED=1
"""

import asyncio
import contextlib

from app.bot.app import run_bot
from app.core.config import settings
from app.core.logging import setup_logging
from app.db.migrations.repair import main as repair_schema
from app.db.session import init_engine
from app.scheduler.worker import run_scheduler


async def main() -> None:
    setup_logging()
    # Idempotent schema safety net (useful on Railway when alembic didn't run DDL).
    repair_schema()
    init_engine(settings.database_url)

    scheduler_task = None
    if settings.scheduler_enabled:
        scheduler_task = asyncio.create_task(run_scheduler(), name="scheduler")

    try:
        await run_bot()
    finally:
        if scheduler_task:
            scheduler_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await scheduler_task


if __name__ == "__main__":
    asyncio.run(main())
