"""make referral_earnings.payment_id nullable

Revision ID: 0009_referral_earnings_payment_id_nullable
Revises: 0008_add_status_to_referrals
Create Date: 2026-02-05
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0009_referral_earnings_payment_id_nullable"
down_revision = "0008_add_status_to_referrals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Allow manual/admin mint operations to create earnings rows without an
    # underlying payment record.
    op.alter_column(
        "referral_earnings",
        "payment_id",
        existing_type=sa.Integer(),
        nullable=True,
    )


def downgrade() -> None:
    # WARNING: this will fail if there are rows with NULL payment_id.
    op.alter_column(
        "referral_earnings",
        "payment_id",
        existing_type=sa.Integer(),
        nullable=False,
    )
