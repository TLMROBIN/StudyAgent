"""repair message assets column on drifted sqlite deployments"""

from alembic import op
import sqlalchemy as sa


revision = "20260621_0014"
down_revision = "20260621_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("messages"):
        return
    columns = {column["name"] for column in inspector.get_columns("messages")}
    if "assets" not in columns:
        op.add_column("messages", sa.Column("assets", sa.JSON(), nullable=False, server_default="[]"))


def downgrade() -> None:
    # This is a repair-only migration for deployments that were accidentally
    # stamped past 20260621_0011. The original 0011 migration owns the column.
    pass
