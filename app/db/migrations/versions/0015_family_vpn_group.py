"""family vpn group and profiles

Revision ID: 0015_family_vpn_group
Revises: 0014_message_audit
Create Date: 2026-03-17
"""

from alembic import op
import sqlalchemy as sa


revision = "0015_family_vpn_group"
down_revision = "0014_message_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "family_vpn_groups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("owner_tg_id", sa.BigInteger(), nullable=False, unique=True, index=True),
        sa.Column("seats_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("billing_opt_in", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_family_vpn_groups_owner_tg_id", "family_vpn_groups", ["owner_tg_id"], unique=True)

    op.create_table(
        "family_vpn_profiles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("owner_tg_id", sa.BigInteger(), nullable=False),
        sa.Column("slot_no", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=64), nullable=True),
        sa.Column("vpn_peer_id", sa.Integer(), sa.ForeignKey("vpn_peers.id"), nullable=True),
        sa.Column("is_paused", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_family_vpn_profiles_owner_tg_id", "family_vpn_profiles", ["owner_tg_id"], unique=False)
    op.create_index(
        "ux_family_vpn_profiles_owner_slot",
        "family_vpn_profiles",
        ["owner_tg_id", "slot_no"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ux_family_vpn_profiles_owner_slot", table_name="family_vpn_profiles")
    op.drop_index("ix_family_vpn_profiles_owner_tg_id", table_name="family_vpn_profiles")
    op.drop_table("family_vpn_profiles")

    op.drop_index("ix_family_vpn_groups_owner_tg_id", table_name="family_vpn_groups")
    op.drop_table("family_vpn_groups")
