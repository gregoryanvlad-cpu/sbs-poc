"""region vpn sessions

Revision ID: 0012_region_vpn_sessions
Revises: 0011_add_vpn_peer_server_code
Create Date: 2026-02-16

"""

from alembic import op
import sqlalchemy as sa


revision = "0012_region_vpn_sessions"
down_revision = "0011_add_vpn_peer_server_code"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "region_vpn_sessions",
        sa.Column("tg_id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("active_ip", sa.String(length=64), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_switch_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_region_vpn_sessions_tg_id", "region_vpn_sessions", ["tg_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_region_vpn_sessions_tg_id", table_name="region_vpn_sessions")
    op.drop_table("region_vpn_sessions")
