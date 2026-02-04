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


def ensure_referrals_schema() -> None:
    """Best-effort schema fixer for referrals.

    This runs on startup in some deployments where alembic migrations may not
    have been applied yet.
    """
    url = os.getenv("DATABASE_URL") or os.getenv("RAILWAY_DATABASE_URL")
    if not url:
        return

    engine = create_engine(url, future=True)

    with engine.begin() as conn:
        # users columns
        try:
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS ref_code VARCHAR(32)"))
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS referred_by_tg_id BIGINT"))
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS referred_at TIMESTAMPTZ"))
        except Exception:
            pass

        # referrals
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS referrals (
                    id SERIAL PRIMARY KEY,
                    referrer_tg_id BIGINT NOT NULL,
                    referred_tg_id BIGINT NOT NULL UNIQUE,
                    first_payment_id INTEGER,
                    created_at TIMESTAMPTZ DEFAULT now(),
                    activated_at TIMESTAMPTZ
                )
                """
            )
        )

        # payout requests
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS payout_requests (
                    id SERIAL PRIMARY KEY,
                    tg_id BIGINT NOT NULL,
                    amount_rub INTEGER NOT NULL,
                    requisites TEXT,
                    status VARCHAR(16) DEFAULT 'created',
                    note TEXT,
                    created_at TIMESTAMPTZ DEFAULT now(),
                    processed_at TIMESTAMPTZ
                )
                """
            )
        )

        # referral earnings
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS referral_earnings (
                    id SERIAL PRIMARY KEY,
                    referrer_tg_id BIGINT NOT NULL,
                    referred_tg_id BIGINT NOT NULL,
                    payment_id INTEGER NOT NULL,
                    amount_rub INTEGER NOT NULL,
                    percent INTEGER NOT NULL,
                    earned_rub INTEGER NOT NULL,
                    status VARCHAR(16) DEFAULT 'pending',
                    available_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ DEFAULT now(),
                    paid_at TIMESTAMPTZ,
                    payout_request_id INTEGER
                )
                """
            )
        )


def main() -> None:
    try:
        ensure_yandex_membership_notification_columns()
        ensure_job_state_table()
        ensure_referrals_schema()
    except Exception:
        return


if __name__ == "__main__":
    main()
