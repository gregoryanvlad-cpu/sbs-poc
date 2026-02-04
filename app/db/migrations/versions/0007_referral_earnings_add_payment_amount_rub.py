"""add payment_amount_rub to referral_earnings if missing

Revision ID: 0007_referral_earnings_add_payment_amount_rub
Revises: 0006_referrals
Create Date: 2026-02-04
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0007_referral_earnings_add_payment_amount_rub"
down_revision = "0006_referrals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)

    tables = set(insp.get_table_names())
    if "referral_earnings" not in tables:
        # If referrals migration wasn't applied at all,
        # let normal chain handle it (upgrade head should create the table earlier).
        return

    cols = [c["name"] for c in insp.get_columns("referral_earnings")]
    if "payment_amount_rub" not in cols:
        op.add_column(
            "referral_earnings",
            sa.Column("payment_amount_rub", sa.Integer(), nullable=False, server_default="0"),
        )
        op.alter_column("referral_earnings", "payment_amount_rub", server_default=None)


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    tables = set(insp.get_table_names())
    if "referral_earnings" not in tables:
        return

    cols = [c["name"] for c in insp.get_columns("referral_earnings")]
    if "payment_amount_rub" in cols:
        op.drop_column("referral_earnings", "payment_amount_rub")
