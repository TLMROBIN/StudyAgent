"""add llm provider configs"""

from alembic import op
import sqlalchemy as sa

revision = "20260414_0004"
down_revision = "20260413_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "llm_provider_configs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("base_url", sa.String(length=255), nullable=False),
        sa.Column("api_key", sa.String(length=512), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("is_fallback", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_llm_provider_configs_is_active"), "llm_provider_configs", ["is_active"], unique=False)
    op.create_index(op.f("ix_llm_provider_configs_is_fallback"), "llm_provider_configs", ["is_fallback"], unique=False)
    op.create_index(op.f("ix_llm_provider_configs_name"), "llm_provider_configs", ["name"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_llm_provider_configs_name"), table_name="llm_provider_configs")
    op.drop_index(op.f("ix_llm_provider_configs_is_fallback"), table_name="llm_provider_configs")
    op.drop_index(op.f("ix_llm_provider_configs_is_active"), table_name="llm_provider_configs")
    op.drop_table("llm_provider_configs")
