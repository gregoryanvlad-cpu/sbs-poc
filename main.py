"""
Project entrypoint.

Bot + scheduler can run in one process (default), or separately.

- BOT service: SCHEDULER_ENABLED=0
- WORKER service: SCHEDULER_ENABLED=1
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import subprocess
import sys

from app.bot.app import run_bot
from app.core.config import settings
from app.core.logging import setup_logging
from app.db.session import init_engine
from app.scheduler.worker import run_scheduler

log = logging.getLogger(__name__)


def _run_alembic_upgrade_head_best_effort() -> None:
    """
    Railway-safe migrations runner.

    We don't rely on Railway console.
    We just run: python -m alembic upgrade head

    If it fails, we log and continue so the service can still start.
    (But DB schema may be outdated, which can break parts of the bot.)
    """
    try:
        # -m alembic uses your alembic.ini and env.py from the project.
        subprocess.check_call([sys.executable, "-m", "alembic", "upgrade", "head"])
        log.info("✅ Alembic migrations applied: upgrade head")
    except FileNotFoundError:
        log.exception("❌ Alembic is not installed / not found. Skipping migrations.")
    except subprocess.CalledProcessError:
        log.exception("❌ Alembic upgrade head failed. Continuing without migrations.")


async def main() -> None:
    setup_logging()

    # 1) Init DB engine (needed for app)
    init_engine(settings.database_url)

    # 2) Apply migrations at boot (best-effort)
    _run_alembic_upgrade_head_best_effort()

    # 3) Start scheduler if enabled
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
