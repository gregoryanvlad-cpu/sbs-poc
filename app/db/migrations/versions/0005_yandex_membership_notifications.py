from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "0005_yandex_membership_notifications"
down_revision = "0004_manual_yandex_invite_slots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add notification bookkeeping columns.

    This migration is intentionally **idempotent**.

    Why:
    - In Railway (and other managed PG setups) the schema may already have been
      repaired/updated by a one-off script (or a previous partially-applied deploy).
    - A non-idempotent op.add_column would then crash with DuplicateColumn.
    """

    bind = op.get_bind()
    insp = inspect(bind)
    existing_cols = {c["name"] for c in insp.get_columns("yandex_memberships")}

    to_add = [
        ("notified_7d_at", sa.DateTime(timezone=True)),
        ("notified_3d_at", sa.DateTime(timezone=True)),
        ("notified_1d_at", sa.DateTime(timezone=True)),
        ("removed_at", sa.DateTime(timezone=True)),
    ]

    for name, coltype in to_add:
        if name in existing_cols:
            continue
        op.add_column("yandex_memberships", sa.Column(name, coltype, nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    existing_cols = {c["name"] for c in insp.get_columns("yandex_memberships")}

    for name in ("removed_at", "notified_1d_at", "notified_3d_at", "notified_7d_at"):
        if name in existing_cols:
            op.drop_column("yandex_memberships", name)
