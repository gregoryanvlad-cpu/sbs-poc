"""add expires_at to family_vpn_profiles

Revision ID: 0017_family_profile_expires_at
Revises: 6d6a44f9ff41
Create Date: 2026-03-29
"""

from alembic import op
import sqlalchemy as sa

revision = "0017_family_profile_expires_at"
down_revision = "6d6a44f9ff41"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("family_vpn_profiles", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(
        """
        UPDATE family_vpn_profiles fp
        SET expires_at = grp.active_until
        FROM family_vpn_groups grp
        WHERE grp.owner_tg_id = fp.owner_tg_id
          AND fp.expires_at IS NULL
        """
    )


def downgrade() -> None:
    op.drop_column("family_vpn_profiles", "expires_at")
