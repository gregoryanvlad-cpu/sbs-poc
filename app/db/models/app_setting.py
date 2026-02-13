"""add app_settings table for runtime config

Revision ID: 0010_app_settings
Revises: 0009_referral_earnings_payment_id_nullable
Create Date: 2026-02-13
"""

from alembic import op
import sqlalchemy as sa


revision = "0010_app_settings"
down_revision = "0009_referral_earnings_payment_id_nullable"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(length=128), primary_key=True, nullable=False),
        sa.Column("int_value", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("app_settings")
