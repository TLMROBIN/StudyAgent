"""Add chat message attachments.

Revision ID: 20260413_0003
Revises: 20260412_0002
Create Date: 2026-04-13 19:50:00
"""

from alembic import op
import sqlalchemy as sa

revision = "20260413_0003"
down_revision = "20260412_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_message_attachments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("message_id", sa.Integer(), sa.ForeignKey("messages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("owner_student_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("storage_key", sa.String(length=500), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("mime_type", sa.String(length=100), nullable=False),
        sa.Column("file_size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("ocr_status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("ocr_confidence", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("message_id", name="uq_chat_message_attachments_message_id"),
    )
    op.create_index("ix_chat_message_attachments_message_id", "chat_message_attachments", ["message_id"])
    op.create_index("ix_chat_message_attachments_owner_student_id", "chat_message_attachments", ["owner_student_id"])
    op.create_index("ix_chat_message_attachments_sha256", "chat_message_attachments", ["sha256"])


def downgrade() -> None:
    op.drop_index("ix_chat_message_attachments_sha256", table_name="chat_message_attachments")
    op.drop_index("ix_chat_message_attachments_owner_student_id", table_name="chat_message_attachments")
    op.drop_index("ix_chat_message_attachments_message_id", table_name="chat_message_attachments")
    op.drop_table("chat_message_attachments")
