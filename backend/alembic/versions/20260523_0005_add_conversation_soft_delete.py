"""add conversation soft delete marker"""

from alembic import op
import sqlalchemy as sa

revision = "20260523_0005"
down_revision = "20260414_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("deleted_by_student_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        op.f("ix_conversations_deleted_by_student_at"),
        "conversations",
        ["deleted_by_student_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_conversations_deleted_by_student_at"), table_name="conversations")
    op.drop_column("conversations", "deleted_by_student_at")
