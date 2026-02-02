"""Add notification bookkeeping fields to yandex_memberships.

This migration is intentionally **idempotent**:
some deployments might have already created these columns via a hotfix
or a partially-applied migration. In that case we must not fail with
DuplicateColumn.

Also important: the Alembic revision id is kept **short** (<= 32 chars)
to avoid issues when alembic_version.version_num is VARCHAR(32).
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision = "0005_notify_fields"
down_revision = "0004_manual_yandex_invite_slots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    existing = {c["name"] for c in insp.get_columns("yandex_memberships")}

    cols = [
        ("notified_7d_at", sa.DateTime(timezone=True)),
        ("notified_3d_at", sa.DateTime(timezone=True)),
        ("notified_1d_at", sa.DateTime(timezone=True)),
        ("removed_at", sa.DateTime(timezone=True)),
    ]

    for name, coltype in cols:
        if name in existing:
            continue
        op.add_column("yandex_memberships", sa.Column(name, coltype, nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    existing = {c["name"] for c in insp.get_columns("yandex_memberships")}

    for name in ("removed_at", "notified_1d_at", "notified_3d_at", "notified_7d_at"):
        if name in existing:
            op.drop_column("yandex_memberships", name)
