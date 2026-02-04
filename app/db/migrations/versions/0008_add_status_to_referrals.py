"""add status to referrals

Revision ID: 0008_add_status_to_referrals
Revises: 0007_referral_earnings_add_payment_amount_rub
Create Date: 2026-02-04
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0008_add_status_to_referrals"
down_revision = "0007_referral_earnings_add_payment_amount_rub"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "referrals",
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="active",
        ),
    )

    # safety for existing rows
    op.execute("UPDATE referrals SET status='active' WHERE status IS NULL")


def downgrade():
    op.drop_column("referrals", "status")
