"""Secondary bot entrypoint (player gateway).

Run this file in a separate Railway service (kinoteka-player).

Required env vars in that service:
 - DATABASE_URL (reference to the shared Postgres)
 - PLAYER_BOT_TOKEN (or BOT_TOKEN)
 - OWNER_TG_ID (any digits; used by shared config loader)
 - MAIN_BOT_USERNAME (e.g. sbsconnect_bot)
 - PLAYER_WHITELIST_DOMAINS (comma-separated)
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys

from app.core.logging import setup_logging
from app.core.config import settings
from app.db.session import init_engine
from app.player_bot.app import run_player_bot

log = logging.getLogger(__name__)


def _run_alembic_upgrade_head_best_effort() -> None:
    """Apply migrations at boot (best-effort)."""
    try:
        subprocess.check_call([sys.executable, "-m", "alembic", "upgrade", "head"])
        log.info("✅ Alembic migrations applied: upgrade head")
    except Exception:
        # best-effort; do not crash player bot
        log.exception("❌ Alembic upgrade head failed. Continuing without migrations.")


async def main() -> None:
    setup_logging()
    init_engine(settings.database_url)
    _run_alembic_upgrade_head_best_effort()

    bot, dp = run_player_bot()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
