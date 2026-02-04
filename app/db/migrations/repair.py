from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def ensure_referrals_schema(session: AsyncSession) -> None:
    """Idempotent schema repair for referral tables.

    Why this exists:
    - project sometimes relies on "repair" (not Alembic) on startup
    - older repair created referral_earnings.amount_rub (wrong)
    - current code expects referral_earnings.payment_amount_rub

    This function:
    1) Creates tables if missing (with current columns)
    2) If referral_earnings exists but has old schema, heals it
    3) Ensures indexes exist

    Safe to run on every startup.
    """

    async def _table_exists(name: str) -> bool:
        q = text(
            """
            SELECT EXISTS(
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema='public' AND table_name=:name
            )
            """
        )
        return bool((await session.execute(q, {"name": name})).scalar())

    async def _column_exists(table: str, col: str) -> bool:
        q = text(
            """
            SELECT EXISTS(
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema='public' AND table_name=:t AND column_name=:c
            )
            """
        )
        return bool((await session.execute(q, {"t": table, "c": col})).scalar())

    # ---------- referrals ----------
    if not await _table_exists("referrals"):
        await session.execute(
            text(
                """
                CREATE TABLE referrals (
                    id SERIAL PRIMARY KEY,
                    referrer_tg_id BIGINT NOT NULL,
                    referred_tg_id BIGINT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    activated_at TIMESTAMPTZ NULL,
                    UNIQUE (referred_tg_id)
                );
                CREATE INDEX IF NOT EXISTS ix_referrals_referrer ON referrals(referrer_tg_id);
                CREATE INDEX IF NOT EXISTS ix_referrals_referred ON referrals(referred_tg_id);
                """
            )
        )

    # ---------- referral_earnings ----------
    if not await _table_exists("referral_earnings"):
        await session.execute(
            text(
                """
                CREATE TABLE referral_earnings (
                    id SERIAL PRIMARY KEY,
                    referrer_tg_id BIGINT NOT NULL,
                    referred_tg_id BIGINT NOT NULL,
                    payment_id BIGINT NULL,
                    payment_amount_rub INTEGER NOT NULL DEFAULT 0,
                    percent INTEGER NOT NULL DEFAULT 0,
                    amount_rub INTEGER NOT NULL DEFAULT 0,
                    status VARCHAR(32) NOT NULL DEFAULT 'pending',
                    hold_until TIMESTAMPTZ NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS ix_referral_earnings_referrer ON referral_earnings(referrer_tg_id);
                CREATE INDEX IF NOT EXISTS ix_referral_earnings_referred ON referral_earnings(referred_tg_id);
                CREATE INDEX IF NOT EXISTS ix_referral_earnings_status ON referral_earnings(status);
                """
            )
        )
    else:
        # heal old schema: add payment_amount_rub if missing
        has_payment_amount = await _column_exists("referral_earnings", "payment_amount_rub")
        if not has_payment_amount:
            await session.execute(
                text("ALTER TABLE referral_earnings ADD COLUMN IF NOT EXISTS payment_amount_rub INTEGER NULL")
            )

            # If old column exists, copy. In old repair the column amount_rub was effectively "payment amount".
            if await _column_exists("referral_earnings", "amount_rub"):
                await session.execute(
                    text(
                        """
                        UPDATE referral_earnings
                        SET payment_amount_rub = COALESCE(payment_amount_rub, amount_rub)
                        WHERE payment_amount_rub IS NULL;
                        """
                    )
                )

            await session.execute(text("ALTER TABLE referral_earnings ALTER COLUMN payment_amount_rub SET DEFAULT 0"))
            await session.execute(text("UPDATE referral_earnings SET payment_amount_rub = 0 WHERE payment_amount_rub IS NULL"))
            await session.execute(text("ALTER TABLE referral_earnings ALTER COLUMN payment_amount_rub SET NOT NULL"))

        # indexes (safe)
        await session.execute(text("CREATE INDEX IF NOT EXISTS ix_referral_earnings_referrer ON referral_earnings(referrer_tg_id)"))
        await session.execute(text("CREATE INDEX IF NOT EXISTS ix_referral_earnings_referred ON referral_earnings(referred_tg_id)"))
        await session.execute(text("CREATE INDEX IF NOT EXISTS ix_referral_earnings_status ON referral_earnings(status)"))

    # ---------- payout_requests ----------
    if not await _table_exists("payout_requests"):
        await session.execute(
            text(
                """
                CREATE TABLE payout_requests (
                    id SERIAL PRIMARY KEY,
                    tg_id BIGINT NOT NULL,
                    amount_rub INTEGER NOT NULL,
                    status VARCHAR(32) NOT NULL DEFAULT 'pending',
                    payout_method VARCHAR(64) NULL,
                    payout_details TEXT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    decided_at TIMESTAMPTZ NULL,
                    decided_by_tg_id BIGINT NULL,
                    admin_comment TEXT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_payout_requests_tg_id ON payout_requests(tg_id);
                CREATE INDEX IF NOT EXISTS ix_payout_requests_status ON payout_requests(status);
                """
            )
        )
    else:
        await session.execute(text("CREATE INDEX IF NOT EXISTS ix_payout_requests_tg_id ON payout_requests(tg_id)"))
        await session.execute(text("CREATE INDEX IF NOT EXISTS ix_payout_requests_status ON payout_requests(status)"))

    await session.commit()
