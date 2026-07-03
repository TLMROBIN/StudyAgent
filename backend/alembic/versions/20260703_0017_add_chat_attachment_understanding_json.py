"""add chat attachment understanding json"""

from alembic import op
import sqlalchemy as sa


revision = "20260703_0017"
down_revision = "20260701_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("chat_message_attachments"):
        return
    column_names = {column["name"] for column in inspector.get_columns("chat_message_attachments")}
    if "understanding_json" in column_names:
        return
    op.add_column("chat_message_attachments", sa.Column("understanding_json", sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("chat_message_attachments"):
        return
    column_names = {column["name"] for column in inspector.get_columns("chat_message_attachments")}
    if "understanding_json" not in column_names:
        return
    op.drop_column("chat_message_attachments", "understanding_json")
