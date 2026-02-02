"""fix alembic_version length

Revision ID: 0000_fix_alembic_version_length
Revises: 
Create Date: 2026-02-02
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0000_fix_alembic_version_length"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # безопасно расширяем колонку alembic_version.version_num
    op.execute(
        """
        ALTER TABLE alembic_version
        ALTER COLUMN version_num TYPE VARCHAR(128)
        """
    )


def downgrade():
    # откат не делаем — уменьшать опасно
    pass
