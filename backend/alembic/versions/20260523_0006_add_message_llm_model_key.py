"""add message llm model key"""

from alembic import op
import sqlalchemy as sa

revision = "20260523_0006"
down_revision = "20260523_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("llm_model_key", sa.String(length=64), nullable=True))
    op.create_index(op.f("ix_messages_llm_model_key"), "messages", ["llm_model_key"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_messages_llm_model_key"), table_name="messages")
    op.drop_column("messages", "llm_model_key")
