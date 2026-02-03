"""Small idempotent schema repair run at boot.

We use this as a safety net in case the database was previously *stamped* to a
new alembic revision without executing the DDL. In that case the DB can be
missing columns expected by the application and the bot crashes.

This module is safe to run multiple times.
"""

from __future__ import annotations

import os

from sqlalchemy import create_engine, inspect, text


def _get_sync_db_url() -> str | None:
    raw = (os.getenv("DATABASE_URL") or "").strip()
    if not raw:
        return None
    if raw.startswith("postgres://"):
        raw = "postgresql://" + raw[len("postgres://") :]
    if raw.startswith("postgresql+asyncpg://"):
        raw = "postgresql://" + raw[len("postgresql+asyncpg://") :]
    return raw


def ensure_yandex_membership_notification_columns() -> None:
    """Ensure notification tracking columns exist on yandex_memberships."""
    url = _get_sync_db_url()
    if not url:
        return

    engine = create_engine(url, future=True)
    insp = inspect(engine)

    try:
        cols = {c["name"] for c in insp.get_columns("yandex_memberships")}
    except Exception:
        return

    wanted = {
        "notified_7d_at": "TIMESTAMPTZ",
        "notified_3d_at": "TIMESTAMPTZ",
        "notified_1d_at": "TIMESTAMPTZ",
        "removed_at": "TIMESTAMPTZ",
        "kick_snoozed_until": "TIMESTAMPTZ",
    }

    missing = [name for name in wanted.keys() if name not in cols]
    if not missing:
        return

    with engine.begin() as conn:
        for name in missing:
            conn.execute(
                text(f'ALTER TABLE yandex_memberships ADD COLUMN IF NOT EXISTS "{name}" {wanted[name]}')
            )


def ensure_job_state_table() -> None:
    """Ensure small key/value table used by scheduler to de-duplicate daily jobs.

    The scheduler reads/writes this table best-effort. Without it, the bot still works,
    but the daily admin report can be sent more than once.
    """
    url = _get_sync_db_url()
    if not url:
        return

    engine = create_engine(url, future=True)

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS job_state (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TIMESTAMPTZ DEFAULT now()
                )
                """
            )
        )


def main() -> None:
    try:
        ensure_yandex_membership_notification_columns()
        ensure_job_state_table()
    except Exception:
        return


if __name__ == "__main__":
    main()
