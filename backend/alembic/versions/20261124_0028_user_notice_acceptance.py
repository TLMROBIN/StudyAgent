"""Track user notice acceptance version and timestamp on users."""
from alembic import op
import sqlalchemy as sa

revision = "20261124_0028"
down_revision = "20261123_0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("users")}
    if "user_notice_accepted_version" not in columns:
        op.add_column("users", sa.Column("user_notice_accepted_version", sa.String(32), nullable=True))
    if "user_notice_accepted_at" not in columns:
        op.add_column("users", sa.Column("user_notice_accepted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_column("user_notice_accepted_at")
        batch.drop_column("user_notice_accepted_version")
