"""add student feedback archive"""

from alembic import op
import sqlalchemy as sa


revision = "20260622_0015"
down_revision = "20260621_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("student_feedback"):
        return
    columns = {column["name"] for column in inspector.get_columns("student_feedback")}
    if "archived_at" not in columns:
        op.add_column("student_feedback", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
        op.create_index(op.f("ix_student_feedback_archived_at"), "student_feedback", ["archived_at"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("student_feedback"):
        return
    columns = {column["name"] for column in inspector.get_columns("student_feedback")}
    if "archived_at" in columns:
        index_names = {index["name"] for index in inspector.get_indexes("student_feedback")}
        if op.f("ix_student_feedback_archived_at") in index_names:
            op.drop_index(op.f("ix_student_feedback_archived_at"), table_name="student_feedback")
        op.drop_column("student_feedback", "archived_at")
