"""initial

Revision ID: 0001_initial
Revises:
Create Date: 2026-01-24
"""

from alembic import op
import sqlalchemy as sa


revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("tg_id", sa.BigInteger(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
    )

    op.create_table(
        "subscriptions",
        sa.Column("tg_id", sa.BigInteger(), primary_key=True),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.ForeignKeyConstraint(["tg_id"], ["users.tg_id"], ondelete="CASCADE"),
    )

    op.create_table(
        "payments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tg_id", sa.BigInteger(), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=8), server_default="RUB", nullable=False),
        sa.Column("provider", sa.String(length=32), server_default="mock", nullable=False),
        sa.Column("status", sa.String(length=16), server_default="success", nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("period_days", sa.Integer(), server_default="30", nullable=False),
        sa.Column("period_months", sa.Integer(), server_default="1", nullable=False),
        sa.Column("provider_payment_id", sa.String(length=128), nullable=True, unique=True),
    )
    op.create_index("ix_payments_tg_id", "payments", ["tg_id"], unique=False)

    op.create_table(
        "vpn_peers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tg_id", sa.BigInteger(), nullable=False),
        sa.Column("client_public_key", sa.String(length=128), nullable=False),
        sa.Column("client_private_key_enc", sa.Text(), nullable=False),
        sa.Column("client_ip", sa.String(length=64), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rotation_reason", sa.String(length=32), nullable=True),
    )
    op.create_index("ix_vpn_peers_tg_id", "vpn_peers", ["tg_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_vpn_peers_tg_id", table_name="vpn_peers")
    op.drop_table("vpn_peers")
    op.drop_index("ix_payments_tg_id", table_name="payments")
    op.drop_table("payments")
    op.drop_table("subscriptions")
    op.drop_table("users")
