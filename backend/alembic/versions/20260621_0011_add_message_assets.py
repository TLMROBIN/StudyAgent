"""add message assets"""

from alembic import op
import sqlalchemy as sa


revision = "20260621_0011"
down_revision = "20260618_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("assets", sa.JSON(), nullable=False, server_default="[]"))


def downgrade() -> None:
    op.drop_column("messages", "assets")
