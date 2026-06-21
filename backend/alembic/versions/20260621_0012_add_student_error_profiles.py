"""add student error profiles"""

from alembic import op
import sqlalchemy as sa


revision = "20260621_0012"
down_revision = "20260621_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "student_error_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("student_id", sa.Integer(), nullable=False),
        sa.Column("subject", sa.String(length=32), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=True),
        sa.Column("message_id", sa.Integer(), nullable=True),
        sa.Column("knowledge_point", sa.String(length=255), nullable=True),
        sa.Column("error_type", sa.String(length=64), nullable=False),
        sa.Column("evidence_text", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["student_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_student_error_events_student_id"), "student_error_events", ["student_id"], unique=False)
    op.create_index(op.f("ix_student_error_events_subject"), "student_error_events", ["subject"], unique=False)
    op.create_index(op.f("ix_student_error_events_conversation_id"), "student_error_events", ["conversation_id"], unique=False)
    op.create_index(op.f("ix_student_error_events_message_id"), "student_error_events", ["message_id"], unique=False)
    op.create_index(op.f("ix_student_error_events_knowledge_point"), "student_error_events", ["knowledge_point"], unique=False)
    op.create_index(op.f("ix_student_error_events_error_type"), "student_error_events", ["error_type"], unique=False)

    op.create_table(
        "student_skill_profiles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("student_id", sa.Integer(), nullable=False),
        sa.Column("subject", sa.String(length=32), nullable=False),
        sa.Column("profile_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["student_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("student_id", "subject", name="uq_student_skill_profiles_student_subject"),
    )
    op.create_index(op.f("ix_student_skill_profiles_student_id"), "student_skill_profiles", ["student_id"], unique=False)
    op.create_index(op.f("ix_student_skill_profiles_subject"), "student_skill_profiles", ["subject"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_student_skill_profiles_subject"), table_name="student_skill_profiles")
    op.drop_index(op.f("ix_student_skill_profiles_student_id"), table_name="student_skill_profiles")
    op.drop_table("student_skill_profiles")
    op.drop_index(op.f("ix_student_error_events_error_type"), table_name="student_error_events")
    op.drop_index(op.f("ix_student_error_events_knowledge_point"), table_name="student_error_events")
    op.drop_index(op.f("ix_student_error_events_message_id"), table_name="student_error_events")
    op.drop_index(op.f("ix_student_error_events_conversation_id"), table_name="student_error_events")
    op.drop_index(op.f("ix_student_error_events_subject"), table_name="student_error_events")
    op.drop_index(op.f("ix_student_error_events_student_id"), table_name="student_error_events")
    op.drop_table("student_error_events")
