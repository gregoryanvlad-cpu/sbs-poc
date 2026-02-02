from alembic import op
import sqlalchemy as sa

revision = "0005_yandex_membership_notifications"
down_revision = "0004_manual_yandex_invite_slots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "yandex_memberships",
        sa.Column("notified_7d_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "yandex_memberships",
        sa.Column("notified_3d_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "yandex_memberships",
        sa.Column("notified_1d_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "yandex_memberships",
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("yandex_memberships", "removed_at")
    op.drop_column("yandex_memberships", "notified_1d_at")
    op.drop_column("yandex_memberships", "notified_3d_at")
    op.drop_column("yandex_memberships", "notified_7d_at")
