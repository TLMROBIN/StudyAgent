"""Add vision understanding priority flag to LLM model configs.

Revision ID: 20260528_0009
Revises: 20260525_0008
Create Date: 2026-05-28
"""

from alembic import op
import sqlalchemy as sa


revision = "20260528_0009"
down_revision = "20260525_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "llm_model_configs",
        sa.Column("vision_understanding_priority", sa.Boolean(), nullable=False, server_default=sa.text("0")),
    )


def downgrade() -> None:
    op.drop_column("llm_model_configs", "vision_understanding_priority")
