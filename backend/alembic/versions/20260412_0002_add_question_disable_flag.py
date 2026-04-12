"""Add reversible disable flag for knowledge chunks.

Revision ID: 20260412_0002
Revises: 20260401_0001
Create Date: 2026-04-12 15:50:00
"""

from alembic import op
import sqlalchemy as sa

revision = "20260412_0002"
down_revision = "20260401_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "knowledge_chunks",
        sa.Column(
            "is_disabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.create_index(
        "ix_knowledge_chunks_is_disabled",
        "knowledge_chunks",
        ["is_disabled"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_knowledge_chunks_is_disabled", table_name="knowledge_chunks")
    op.drop_column("knowledge_chunks", "is_disabled")
