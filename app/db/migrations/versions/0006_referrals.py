"""referrals

Revision ID: 0006_referrals
Revises: 6d6a44f9ff41
Create Date: 2026-02-03

"""

from alembic import op
import sqlalchemy as sa


revision = "0006_referrals"
down_revision = "6d6a44f9ff41"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # User: referral metadata
    op.add_column("users", sa.Column("ref_code", sa.String(length=32), nullable=True))
    op.add_column("users", sa.Column("referred_by_tg_id", sa.BigInteger(), nullable=True))
    op.add_column("users", sa.Column("referred_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_users_ref_code", "users", ["ref_code"], unique=True)
    op.create_index("ix_users_referred_by_tg_id", "users", ["referred_by_tg_id"], unique=False)

    # Referrals
    op.create_table(
        "referrals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("referrer_tg_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("referred_tg_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("first_payment_id", sa.Integer(), nullable=True, index=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("referred_tg_id", name="uq_referrals_referred_tg_id"),
    )

    # Referral earnings ledger
    op.create_table(
        "referral_earnings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("referrer_tg_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("referred_tg_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("payment_id", sa.Integer(), nullable=False, index=True),
        sa.Column("payment_amount_rub", sa.Integer(), nullable=False),
        sa.Column("percent", sa.Integer(), nullable=False),
        sa.Column("earned_rub", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payout_request_id", sa.Integer(), nullable=True, index=True),
        sa.UniqueConstraint("payment_id", "referrer_tg_id", name="uq_referral_earnings_payment_referrer"),
    )

    # Payout requests
    op.create_table(
        "payout_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tg_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("amount_rub", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="created"),
        sa.Column("requisites", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("payout_requests")
    op.drop_table("referral_earnings")
    op.drop_table("referrals")
    op.drop_index("ix_users_referred_by_tg_id", table_name="users")
    op.drop_index("ix_users_ref_code", table_name="users")
    op.drop_column("users", "referred_at")
    op.drop_column("users", "referred_by_tg_id")
    op.drop_column("users", "ref_code")
