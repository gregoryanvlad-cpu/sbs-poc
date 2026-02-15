"""add server_code to vpn_peers for multi-location

Revision ID: 0011_add_vpn_peer_server_code
Revises: 0010_app_settings
Create Date: 2026-02-15
"""

from alembic import op
import sqlalchemy as sa

revision = "0011_add_vpn_peer_server_code"
down_revision = "0010_app_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable for backwards compatibility.
    op.add_column("vpn_peers", sa.Column("server_code", sa.String(length=8), nullable=True))
    op.create_index("ix_vpn_peers_server_code", "vpn_peers", ["server_code"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_vpn_peers_server_code", table_name="vpn_peers")
    op.drop_column("vpn_peers", "server_code")
