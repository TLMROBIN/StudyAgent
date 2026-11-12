"""Persist chat delivery failures without counting them as completed turns."""
from alembic import op
import sqlalchemy as sa

revision = "20261112_0025"
down_revision = "20261030_0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("messages")}
    if "delivery_status" not in columns:
        op.add_column(
            "messages",
            sa.Column("delivery_status", sa.String(16), server_default="completed", nullable=False),
        )
    if "failure_reason" not in columns:
        op.add_column("messages", sa.Column("failure_reason", sa.String(500), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("messages") as batch:
        batch.drop_column("failure_reason")
        batch.drop_column("delivery_status")
