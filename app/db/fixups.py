import asyncio
import os
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


def _db_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")

    if url.startswith("postgresql://") and "+asyncpg" not in url:
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("postgres://") and "+asyncpg" not in url:
        url = url.replace("postgres://", "postgresql+asyncpg://", 1)

    return url


async def main() -> None:
    engine = create_async_engine(_db_url(), future=True)

    async with engine.begin() as conn:
        # 1️⃣ add column
        await conn.execute(text("""
            ALTER TABLE payments
            ADD COLUMN IF NOT EXISTS provider_payment_id VARCHAR(128)
        """))

        # 2️⃣ add index
        await conn.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS ux_payments_provider_payment_id
            ON payments(provider_payment_id)
            WHERE provider_payment_id IS NOT NULL
        """))

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
