"""add student incentive ledger and profiles

Revision ID: 20260716_0020
Revises: 20260716_0019
"""

from alembic import op
import sqlalchemy as sa


revision = "20260716_0020"
down_revision = "20260716_0019"
branch_labels = None
depends_on = None


def _index_names(inspector: sa.Inspector, table_name: str) -> set[str]:
    return {index["name"] for index in inspector.get_indexes(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("student_incentive_events"):
        op.create_table(
            "student_incentive_events",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("student_id", sa.Integer(), nullable=False),
            sa.Column("subject", sa.String(length=32), nullable=True),
            sa.Column("conversation_id", sa.Integer(), nullable=True),
            sa.Column("event_type", sa.String(length=32), nullable=False),
            sa.Column("points", sa.Integer(), server_default="0", nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("dedup_key", sa.String(length=128), nullable=True),
            sa.Column("created_by", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["student_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("dedup_key"),
        )
    inspector = sa.inspect(bind)
    if not inspector.has_table("student_incentive_profiles"):
        op.create_table(
            "student_incentive_profiles",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("student_id", sa.Integer(), nullable=False),
            sa.Column("total_points", sa.Integer(), server_default="0", nullable=False),
            sa.Column("level", sa.Integer(), server_default="1", nullable=False),
            sa.Column("current_streak_days", sa.Integer(), server_default="0", nullable=False),
            sa.Column("longest_streak_days", sa.Integer(), server_default="0", nullable=False),
            sa.Column("last_valid_learning_date", sa.Date(), nullable=True),
            sa.Column("daily_points", sa.Integer(), server_default="0", nullable=False),
            sa.Column("daily_points_date", sa.Date(), nullable=True),
            sa.Column("badges", sa.JSON(), nullable=False),
            sa.Column("counters", sa.JSON(), nullable=False),
            sa.Column("last_praise_read_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(["student_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("student_id"),
        )

    event_indexes = _index_names(sa.inspect(bind), "student_incentive_events")
    for name, columns, unique in (
        ("ix_student_incentive_events_student_id", ["student_id"], False),
        ("ix_student_incentive_events_subject", ["subject"], False),
        ("ix_student_incentive_events_conversation_id", ["conversation_id"], False),
        ("ix_student_incentive_events_event_type", ["event_type"], False),
        ("ix_student_incentive_events_student_created", ["student_id", "created_at"], False),
        ("ix_student_incentive_events_student_type", ["student_id", "event_type"], False),
    ):
        if name not in event_indexes:
            op.create_index(name, "student_incentive_events", columns, unique=unique)
    profile_indexes = _index_names(sa.inspect(bind), "student_incentive_profiles")
    if "ix_student_incentive_profiles_student_id" not in profile_indexes:
        op.create_index(
            "ix_student_incentive_profiles_student_id",
            "student_incentive_profiles",
            ["student_id"],
            unique=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("student_incentive_profiles"):
        op.drop_table("student_incentive_profiles")
    if sa.inspect(bind).has_table("student_incentive_events"):
        op.drop_table("student_incentive_events")
