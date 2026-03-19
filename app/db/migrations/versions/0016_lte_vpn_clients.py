"""lte vpn clients

Revision ID: 0016_lte_vpn_clients
Revises: 0015_family_vpn_group
Create Date: 2026-03-19
"""

from alembic import op
import sqlalchemy as sa

revision = "0016_lte_vpn_clients"
down_revision = "0015_family_vpn_group"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "lte_vpn_clients",
        sa.Column("tg_id", sa.BigInteger(), nullable=False),
        sa.Column("uuid", sa.String(length=64), nullable=False),
        sa.Column("email", sa.String(length=128), nullable=False),
        sa.Column("cycle_anchor_end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rate_mbit", sa.Integer(), server_default="25", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["tg_id"], ["users.tg_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("tg_id"),
    )
    op.create_index(op.f("ix_lte_vpn_clients_uuid"), "lte_vpn_clients", ["uuid"], unique=True)
    op.create_index(op.f("ix_lte_vpn_clients_email"), "lte_vpn_clients", ["email"], unique=True)


def downgrade() -> None:
    op.drop_index(op.f("ix_lte_vpn_clients_email"), table_name="lte_vpn_clients")
    op.drop_index(op.f("ix_lte_vpn_clients_uuid"), table_name="lte_vpn_clients")
    op.drop_table("lte_vpn_clients")
