"""Add Telegram profile fields to users.

Revision ID: 0013_add_user_profile_fields
Revises: 0012_region_vpn_sessions
Create Date: 2026-02-16
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0013_add_user_profile_fields"
down_revision = "0012_region_vpn_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("tg_username", sa.String(length=64), nullable=True))
    op.add_column("users", sa.Column("first_name", sa.String(length=128), nullable=True))
    op.add_column("users", sa.Column("last_name", sa.String(length=128), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "last_name")
    op.drop_column("users", "first_name")
    op.drop_column("users", "tg_username")
