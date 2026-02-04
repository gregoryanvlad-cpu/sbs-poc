"""
Project entrypoint.

Bot + scheduler can run in one process (default), or separately.

- BOT service: SCHEDULER_ENABLED=0
- WORKER service: SCHEDULER_ENABLED=1
"""

import asyncio
import contextlib
import subprocess
import sys

from app.bot.app import run_bot
from app.core.config import settings
from app.core.logging import setup_logging
from app.db.migrations.repair import main as repair_schema
from app.db.session import init_engine
from app.scheduler.worker import run_scheduler


def run_alembic() -> None:
    """
    Railway-safe alembic runner.
    Applies ALL migrations on startup.
    """
    try:
        subprocess.check_call(
            [sys.executable, "-m", "alembic", "upgrade", "head"]
        )
    except subprocess.CalledProcessError as e:
        print("❌ Alembic migration failed")
        raise e


async def main() -> None:
    setup_logging()

    # 1️⃣ Apply alembic migrations (CRITICAL)
    run_alembic()

    # 2️⃣ Idempotent schema safety net
    repair_schema()

    # 3️⃣ Init DB engine
    init_engine(settings.database_url)

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
