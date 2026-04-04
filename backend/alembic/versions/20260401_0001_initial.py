"""Initial schema bootstrap.

Revision ID: 20260401_0001
Revises:
Create Date: 2026-04-01 10:30:00
"""

from alembic import op

from backend.database import Base
from backend.models import agent_config, audit_log, conversation, knowledge, user  # noqa: F401

revision = "20260401_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
