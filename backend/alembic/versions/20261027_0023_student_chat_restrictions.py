"""Allow administrators to suspend student Q&A until a specified instant."""
from alembic import op
import sqlalchemy as sa

revision = "20261027_0023"
down_revision = "20261025_0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("users")}
    if "chat_banned_until" not in columns:
        op.add_column("users", sa.Column("chat_banned_until", sa.DateTime(timezone=True), nullable=True))
    if "chat_ban_reason" not in columns:
        op.add_column("users", sa.Column("chat_ban_reason", sa.String(500), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_column("chat_ban_reason")
        batch.drop_column("chat_banned_until")
