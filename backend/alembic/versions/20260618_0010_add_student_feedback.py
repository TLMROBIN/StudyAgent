"""add student feedback"""

from alembic import op
import sqlalchemy as sa


revision = "20260618_0010"
down_revision = "20260528_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "student_feedback",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("student_id", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("reply_content", sa.Text(), nullable=True),
        sa.Column("replied_by", sa.Integer(), nullable=True),
        sa.Column("replied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["replied_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["student_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_student_feedback_student_id"), "student_feedback", ["student_id"], unique=False)
    op.create_index(op.f("ix_student_feedback_replied_at"), "student_feedback", ["replied_at"], unique=False)

    op.create_table(
        "student_feedback_bans",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("student_id", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column("banned_by", sa.Integer(), nullable=True),
        sa.Column("banned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["banned_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["student_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("student_id", name="uq_student_feedback_bans_student_id"),
    )
    op.create_index(op.f("ix_student_feedback_bans_student_id"), "student_feedback_bans", ["student_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_student_feedback_bans_student_id"), table_name="student_feedback_bans")
    op.drop_table("student_feedback_bans")
    op.drop_index(op.f("ix_student_feedback_replied_at"), table_name="student_feedback")
    op.drop_index(op.f("ix_student_feedback_student_id"), table_name="student_feedback")
    op.drop_table("student_feedback")
