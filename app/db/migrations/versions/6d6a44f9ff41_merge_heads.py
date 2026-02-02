"""merge heads

Revision ID: 6d6a44f9ff41
Revises: 0000_fix_alembic_version_length, 0005_notify_fields
Create Date: 2026-02-02 20:32:50.124448

"""

from alembic import op
import sqlalchemy as sa


revision = '6d6a44f9ff41'
down_revision = ('0000_fix_alembic_version_length', '0005_notify_fields')
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
