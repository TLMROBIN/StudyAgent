"""Keep archived student conversations available without cluttering history."""
from alembic import op
import sqlalchemy as sa

revision = "20261030_0024"
down_revision = "20261027_0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("conversations")}
    if "is_archived" not in columns:
        op.add_column("conversations", sa.Column("is_archived", sa.Boolean(), server_default=sa.false(), nullable=False))


def downgrade() -> None:
    with op.batch_alter_table("conversations") as batch:
        batch.drop_column("is_archived")
