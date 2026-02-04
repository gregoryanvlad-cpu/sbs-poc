"""add status to referrals

Revision ID: add_referrals_status
Revises: <ПОСЛЕДНИЙ_REVISION_ID>
Create Date: 2026-02-04
"""

from alembic import op
import sqlalchemy as sa


# !!! ОБЯЗАТЕЛЬНО !!!
revision = "add_referrals_status"
down_revision = "<ПОСЛЕДНИЙ_REVISION_ID>"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "referrals",
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="pending",
        ),
    )


def downgrade():
    op.drop_column("referrals", "status")
