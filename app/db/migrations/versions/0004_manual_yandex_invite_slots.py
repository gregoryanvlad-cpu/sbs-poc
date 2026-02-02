
from alembic import op
import sqlalchemy as sa

revision = "0004_manual_yandex_invite_slots"
down_revision = "0003_user_flow"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "yandex_invite_slots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("yandex_account_id", sa.Integer(), sa.ForeignKey("yandex_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("slot_index", sa.Integer(), nullable=False),
        sa.Column("invite_link", sa.String(length=512), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="free", nullable=False),
        sa.Column("issued_to_tg_id", sa.BigInteger(), nullable=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_yandex_invite_slots_account", "yandex_invite_slots", ["yandex_account_id", "slot_index"], unique=True)

    op.add_column("yandex_memberships", sa.Column("invite_slot_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_yandex_memberships_invite_slot",
        "yandex_memberships",
        "yandex_invite_slots",
        ["invite_slot_id"],
        ["id"],
    )
    op.add_column("yandex_memberships", sa.Column("account_label", sa.String(length=64), nullable=True))
    op.add_column("yandex_memberships", sa.Column("slot_index", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("yandex_memberships", "slot_index")
    op.drop_column("yandex_memberships", "account_label")
    op.drop_constraint("fk_yandex_memberships_invite_slot", "yandex_memberships", type_="foreignkey")
    op.drop_column("yandex_memberships", "invite_slot_id")
    op.drop_index("ix_yandex_invite_slots_account", table_name="yandex_invite_slots")
    op.drop_table("yandex_invite_slots")
