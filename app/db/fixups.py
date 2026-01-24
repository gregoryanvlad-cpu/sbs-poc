"""
One-shot, idempotent DB fixups for production.
This runs at container start so you don't need manual SQL access in Railway UI.

Currently:
- Adds payments.provider_payment_id (nullable) + unique partial index for idempotent webhooks later.
"""
from __future__ import annotations

import os
import sys
from sqlalchemy import create_engine, text


def main() -> int:
    db_url = os.getenv("DATABASE_URL") or os.getenv("DATABASE_PUBLIC_URL") or os.getenv("POSTGRES_URL")
    if not db_url:
        print("DB FIXUPS: DATABASE_URL is not set; skipping.")
        return 0

    # Some platforms provide postgres://; SQLAlchemy prefers postgresql://
    if db_url.startswith("postgres://"):
        db_url = "postgresql://" + db_url[len("postgres://"):]

    engine = create_engine(db_url, pool_pre_ping=True, future=True)

    statements = [
        # provider_payment_id column for idempotent provider webhooks
        """
        ALTER TABLE payments
        ADD COLUMN IF NOT EXISTS provider_payment_id VARCHAR(128);
        """,
        # unique index for non-null provider_payment_id values
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_payments_provider_payment_id
        ON payments(provider_payment_id)
        WHERE provider_payment_id IS NOT NULL;
        """
    ]

    with engine.begin() as conn:
        for stmt in statements:
            conn.execute(text(stmt))
    print("DB FIXUPS: applied successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
