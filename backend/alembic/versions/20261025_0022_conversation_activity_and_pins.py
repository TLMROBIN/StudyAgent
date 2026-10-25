"""Keep conversation activity independent from metadata edits and persist pins."""
from alembic import op
import sqlalchemy as sa

revision = "20261025_0022"
down_revision = "20261023_0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("conversations")}
    if "is_pinned" not in columns:
        op.add_column("conversations", sa.Column("is_pinned", sa.Boolean(), server_default=sa.false(), nullable=False))
    if "last_message_at" not in columns:
        op.add_column("conversations", sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True))
    bind.execute(sa.text("UPDATE conversations SET last_message_at = COALESCE((SELECT MAX(created_at) FROM messages WHERE messages.conversation_id = conversations.id), created_at) WHERE last_message_at IS NULL"))


def downgrade() -> None:
    with op.batch_alter_table("conversations") as batch:
        batch.drop_column("last_message_at")
        batch.drop_column("is_pinned")
