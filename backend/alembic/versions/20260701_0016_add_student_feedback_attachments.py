"""add student feedback attachments"""

from alembic import op
import sqlalchemy as sa


revision = "20260701_0016"
down_revision = "20260622_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("student_feedback_attachments"):
        return
    op.create_table(
        "student_feedback_attachments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("feedback_id", sa.Integer(), nullable=False),
        sa.Column("student_id", sa.Integer(), nullable=False),
        sa.Column("storage_key", sa.String(length=500), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("mime_type", sa.String(length=100), nullable=False),
        sa.Column("file_size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["feedback_id"], ["student_feedback.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["student_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_student_feedback_attachments_feedback_id"), "student_feedback_attachments", ["feedback_id"], unique=False)
    op.create_index(op.f("ix_student_feedback_attachments_student_id"), "student_feedback_attachments", ["student_id"], unique=False)
    op.create_index(op.f("ix_student_feedback_attachments_sha256"), "student_feedback_attachments", ["sha256"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("student_feedback_attachments"):
        return
    index_names = {index["name"] for index in inspector.get_indexes("student_feedback_attachments")}
    for index_name in [
        op.f("ix_student_feedback_attachments_sha256"),
        op.f("ix_student_feedback_attachments_student_id"),
        op.f("ix_student_feedback_attachments_feedback_id"),
    ]:
        if index_name in index_names:
            op.drop_index(index_name, table_name="student_feedback_attachments")
    op.drop_table("student_feedback_attachments")
