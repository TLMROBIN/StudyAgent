"""add conversation active practice"""

from alembic import op
import sqlalchemy as sa


revision = "20260707_0018"
down_revision = "20260703_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("conversations"):
        return
    column_names = {column["name"] for column in inspector.get_columns("conversations")}
    if "active_practice" in column_names:
        return
    op.add_column("conversations", sa.Column("active_practice", sa.JSON(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("conversations"):
        return
    column_names = {column["name"] for column in inspector.get_columns("conversations")}
    if "active_practice" not in column_names:
        return
    op.drop_column("conversations", "active_practice")
