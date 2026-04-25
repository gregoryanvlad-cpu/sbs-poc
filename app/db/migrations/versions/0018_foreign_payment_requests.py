"""foreign payment requests

Revision ID: 0018_foreign_payment_requests
Revises: 0017_family_profile_expires_at
Create Date: 2026-04-25
"""

from alembic import op
import sqlalchemy as sa

revision = "0018_foreign_payment_requests"
down_revision = "0017_family_profile_expires_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "foreign_payment_requests",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("full_name", sa.String(length=255), nullable=True),
        sa.Column("service_key", sa.String(length=32), nullable=False),
        sa.Column("amount_raw", sa.String(length=64), nullable=True),
        sa.Column("fee_percent", sa.Integer(), nullable=True),
        sa.Column("total_raw", sa.String(length=64), nullable=True),
        sa.Column("details", sa.Text(), nullable=False),
        sa.Column("contact", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="new"),
        sa.Column("admin_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_foreign_payment_requests_user_id", "foreign_payment_requests", ["user_id"])
    op.create_index("ix_foreign_payment_requests_service_key", "foreign_payment_requests", ["service_key"])
    op.create_index("ix_foreign_payment_requests_status", "foreign_payment_requests", ["status"])


def downgrade() -> None:
    op.drop_index("ix_foreign_payment_requests_status", table_name="foreign_payment_requests")
    op.drop_index("ix_foreign_payment_requests_service_key", table_name="foreign_payment_requests")
    op.drop_index("ix_foreign_payment_requests_user_id", table_name="foreign_payment_requests")
    op.drop_table("foreign_payment_requests")
