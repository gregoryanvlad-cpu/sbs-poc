"""Add outgoing message audit log.

Revision ID: 0014_message_audit
Revises: 0013_add_user_profile_fields
Create Date: 2026-03-06
"""

from alembic import op
import sqlalchemy as sa


revision = "0014_message_audit"
down_revision = "0013_add_user_profile_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "message_audit",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tg_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.Column("message_id", sa.Integer(), nullable=True),
        sa.Column("text_preview", sa.Text(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_message_audit_tg_id_sent_at", "message_audit", ["tg_id", "sent_at"])


def downgrade() -> None:
    op.drop_index("ix_message_audit_tg_id_sent_at", table_name="message_audit")
    op.drop_table("message_audit")
