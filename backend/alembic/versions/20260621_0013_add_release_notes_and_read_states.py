"""add release notes and read states"""

from alembic import op
import sqlalchemy as sa


revision = "20260621_0013"
down_revision = "20260621_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "student_feedback_read_states",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("student_id", sa.Integer(), nullable=False),
        sa.Column("feedback_id", sa.Integer(), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["feedback_id"], ["student_feedback.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["student_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("student_id", "feedback_id", name="uq_student_feedback_read_states_student_feedback"),
    )
    op.create_index(op.f("ix_student_feedback_read_states_student_id"), "student_feedback_read_states", ["student_id"], unique=False)
    op.create_index(op.f("ix_student_feedback_read_states_feedback_id"), "student_feedback_read_states", ["feedback_id"], unique=False)
    op.create_index(op.f("ix_student_feedback_read_states_read_at"), "student_feedback_read_states", ["read_at"], unique=False)

    op.create_table(
        "release_notes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("is_published", sa.Boolean(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_release_notes_is_published"), "release_notes", ["is_published"], unique=False)
    op.create_index(op.f("ix_release_notes_published_at"), "release_notes", ["published_at"], unique=False)

    op.create_table(
        "release_note_read_states",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("student_id", sa.Integer(), nullable=False),
        sa.Column("release_note_id", sa.Integer(), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["release_note_id"], ["release_notes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["student_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("student_id", "release_note_id", name="uq_release_note_read_states_student_note"),
    )
    op.create_index(op.f("ix_release_note_read_states_student_id"), "release_note_read_states", ["student_id"], unique=False)
    op.create_index(op.f("ix_release_note_read_states_release_note_id"), "release_note_read_states", ["release_note_id"], unique=False)
    op.create_index(op.f("ix_release_note_read_states_read_at"), "release_note_read_states", ["read_at"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_release_note_read_states_read_at"), table_name="release_note_read_states")
    op.drop_index(op.f("ix_release_note_read_states_release_note_id"), table_name="release_note_read_states")
    op.drop_index(op.f("ix_release_note_read_states_student_id"), table_name="release_note_read_states")
    op.drop_table("release_note_read_states")

    op.drop_index(op.f("ix_release_notes_published_at"), table_name="release_notes")
    op.drop_index(op.f("ix_release_notes_is_published"), table_name="release_notes")
    op.drop_table("release_notes")

    op.drop_index(op.f("ix_student_feedback_read_states_read_at"), table_name="student_feedback_read_states")
    op.drop_index(op.f("ix_student_feedback_read_states_feedback_id"), table_name="student_feedback_read_states")
    op.drop_index(op.f("ix_student_feedback_read_states_student_id"), table_name="student_feedback_read_states")
    op.drop_table("student_feedback_read_states")
