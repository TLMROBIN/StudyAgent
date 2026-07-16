"""add immutable teaching roles

Revision ID: 20260716_0019
Revises: 20260707_0018
"""

from alembic import op
import sqlalchemy as sa


revision = "20260716_0019"
down_revision = "20260707_0018"
branch_labels = None
depends_on = None


def _index_names(inspector: sa.Inspector, table_name: str) -> set[str]:
    return {index["name"] for index in inspector.get_indexes(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("agent_roles"):
        op.create_table(
            "agent_roles",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=64), nullable=False),
            sa.Column("display_name", sa.String(length=64), nullable=False),
            sa.Column("emoji", sa.String(length=16), nullable=True),
            sa.Column("description", sa.String(length=255), server_default="", nullable=False),
            sa.Column("subjects", sa.JSON(), nullable=True),
            sa.Column("current_revision_id", sa.Integer(), nullable=True),
            sa.Column("is_enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
            sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
            sa.Column("created_by", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("name"),
        )

    inspector = sa.inspect(bind)
    if not inspector.has_table("agent_role_revisions"):
        op.create_table(
            "agent_role_revisions",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("role_id", sa.Integer(), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("style_config", sa.JSON(), nullable=False),
            sa.Column("renderer_version", sa.String(length=32), nullable=False),
            sa.Column("rendered_prompt", sa.Text(), nullable=False),
            sa.Column("content_hash", sa.String(length=64), nullable=False),
            sa.Column("created_by", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["role_id"], ["agent_roles.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("role_id", "revision", name="uq_agent_role_revisions_role_revision"),
        )

    inspector = sa.inspect(bind)
    role_indexes = _index_names(inspector, "agent_roles")
    for name, columns, unique in (
        ("ix_agent_roles_name", ["name"], True),
        ("ix_agent_roles_current_revision_id", ["current_revision_id"], False),
        ("ix_agent_roles_is_enabled", ["is_enabled"], False),
        ("ix_agent_roles_sort_order", ["sort_order"], False),
    ):
        if name not in role_indexes:
            op.create_index(name, "agent_roles", columns, unique=unique)

    revision_indexes = _index_names(sa.inspect(bind), "agent_role_revisions")
    for name, columns in (
        ("ix_agent_role_revisions_role_id", ["role_id"]),
        ("ix_agent_role_revisions_content_hash", ["content_hash"]),
    ):
        if name not in revision_indexes:
            op.create_index(name, "agent_role_revisions", columns, unique=False)

    inspector = sa.inspect(bind)
    if inspector.has_table("messages"):
        columns = {column["name"] for column in inspector.get_columns("messages")}
        if "agent_role_revision_id" not in columns:
            op.add_column("messages", sa.Column("agent_role_revision_id", sa.Integer(), nullable=True))
        if "agent_role_snapshot" not in columns:
            op.add_column("messages", sa.Column("agent_role_snapshot", sa.JSON(), nullable=True))
        indexes = _index_names(sa.inspect(bind), "messages")
        if "ix_messages_agent_role_revision_id" not in indexes:
            op.create_index("ix_messages_agent_role_revision_id", "messages", ["agent_role_revision_id"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("messages"):
        indexes = _index_names(inspector, "messages")
        if "ix_messages_agent_role_revision_id" in indexes:
            op.drop_index("ix_messages_agent_role_revision_id", table_name="messages")
        columns = {column["name"] for column in sa.inspect(bind).get_columns("messages")}
        if "agent_role_snapshot" in columns:
            op.drop_column("messages", "agent_role_snapshot")
        columns = {column["name"] for column in sa.inspect(bind).get_columns("messages")}
        if "agent_role_revision_id" in columns:
            op.drop_column("messages", "agent_role_revision_id")

    inspector = sa.inspect(bind)
    if inspector.has_table("agent_role_revisions"):
        op.drop_table("agent_role_revisions")
    if sa.inspect(bind).has_table("agent_roles"):
        op.drop_table("agent_roles")
