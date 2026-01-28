
from alembic import op
import sqlalchemy as sa

revision = "0002_yandex"
down_revision = "0001_initial"

def upgrade():
    op.create_table(
        "yandex_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("label", sa.String(64), nullable=False, unique=True),
        sa.Column("status", sa.String(16), server_default="active"),
        sa.Column("max_slots", sa.Integer(), server_default="4"),
        sa.Column("used_slots", sa.Integer(), server_default="0"),
        sa.Column("plus_end_at", sa.DateTime(timezone=True)),
        sa.Column("credentials_ref", sa.String(256)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )

    op.create_table(
        "yandex_memberships",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tg_id", sa.BigInteger(), sa.ForeignKey("users.tg_id", ondelete="CASCADE")),
        sa.Column("yandex_account_id", sa.Integer(), sa.ForeignKey("yandex_accounts.id")),
        sa.Column("yandex_login", sa.String(128), nullable=False),
        sa.Column("status", sa.String(24), server_default="pending"),
        sa.Column("invite_link", sa.String(512)),
        sa.Column("invite_issued_at", sa.DateTime(timezone=True)),
        sa.Column("invite_expires_at", sa.DateTime(timezone=True)),
        sa.Column("reinvite_used", sa.Integer(), server_default="0"),
        sa.Column("coverage_end_at", sa.DateTime(timezone=True)),
        sa.Column("switch_at", sa.DateTime(timezone=True)),
        sa.Column("abuse_strikes", sa.Integer(), server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )

def downgrade():
    op.drop_table("yandex_memberships")
    op.drop_table("yandex_accounts")
