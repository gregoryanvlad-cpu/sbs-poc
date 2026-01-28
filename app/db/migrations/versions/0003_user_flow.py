from alembic import op
import sqlalchemy as sa

revision = "0003_user_flow"
down_revision = "0002_yandex"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "users",
        sa.Column("flow_state", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("flow_data", sa.Text(), nullable=True),
    )


def downgrade():
    op.drop_column("users", "flow_data")
    op.drop_column("users", "flow_state")
