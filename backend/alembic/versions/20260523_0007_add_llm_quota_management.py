"""add llm quota management"""

from alembic import op
import sqlalchemy as sa

revision = "20260523_0007"
down_revision = "20260523_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "llm_provider_accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provider_name", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=64), nullable=False),
        sa.Column("base_url", sa.String(length=255), nullable=False),
        sa.Column("api_key", sa.String(length=512), nullable=False),
        sa.Column("account_billing_type", sa.String(length=16), nullable=False, server_default="pay_as_you_go"),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_llm_provider_accounts_provider_name"), "llm_provider_accounts", ["provider_name"], unique=False)
    op.create_index(op.f("ix_llm_provider_accounts_is_enabled"), "llm_provider_accounts", ["is_enabled"], unique=False)

    op.create_table(
        "llm_model_configs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("model_key", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=80), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("provider_account_id", sa.Integer(), nullable=False),
        sa.Column("provider_model", sa.String(length=128), nullable=False),
        sa.Column("capability_text", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("capability_vision", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("is_fallback", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["provider_account_id"], ["llm_provider_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("model_key"),
    )
    op.create_index(op.f("ix_llm_model_configs_model_key"), "llm_model_configs", ["model_key"], unique=False)
    op.create_index(op.f("ix_llm_model_configs_is_enabled"), "llm_model_configs", ["is_enabled"], unique=False)
    op.create_index(op.f("ix_llm_model_configs_is_primary"), "llm_model_configs", ["is_primary"], unique=False)
    op.create_index(op.f("ix_llm_model_configs_is_fallback"), "llm_model_configs", ["is_fallback"], unique=False)

    op.create_table(
        "llm_quota_policies",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("model_config_id", sa.Integer(), nullable=False),
        sa.Column("billing_mode", sa.String(length=16), nullable=False),
        sa.Column("user_daily_request_limit", sa.Integer(), nullable=True),
        sa.Column("user_daily_token_limit", sa.Integer(), nullable=True),
        sa.Column("school_daily_request_limit", sa.Integer(), nullable=True),
        sa.Column("school_daily_token_limit", sa.Integer(), nullable=True),
        sa.Column("provider_rolling_5h_request_limit", sa.Integer(), nullable=True),
        sa.Column("provider_weekly_request_limit", sa.Integer(), nullable=True),
        sa.Column("max_completion_tokens", sa.Integer(), nullable=True),
        sa.Column("count_cache_hit", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("fail_closed_on_store_error", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["model_config_id"], ["llm_model_configs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("model_config_id"),
    )

    op.create_table(
        "llm_usage_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=True),
        sa.Column("message_id", sa.Integer(), nullable=True),
        sa.Column("request_id", sa.String(length=128), nullable=True),
        sa.Column("model_config_id", sa.Integer(), nullable=False),
        sa.Column("provider_account_id", sa.Integer(), nullable=False),
        sa.Column("model_key", sa.String(length=64), nullable=False),
        sa.Column("provider_name", sa.String(length=64), nullable=False),
        sa.Column("provider_model", sa.String(length=128), nullable=False),
        sa.Column("billing_mode", sa.String(length=32), nullable=False),
        sa.Column("actual_model_key", sa.String(length=64), nullable=True),
        sa.Column("actual_provider_model", sa.String(length=128), nullable=True),
        sa.Column("request_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reasoning_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("prompt_cache_hit_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("prompt_cache_miss_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("estimated", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="local_estimate"),
        sa.Column("policy_snapshot_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("reservation_key", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["model_config_id"], ["llm_model_configs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["provider_account_id"], ["llm_provider_accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("reservation_key"),
    )
    op.create_index(op.f("ix_llm_usage_events_user_id"), "llm_usage_events", ["user_id"], unique=False)
    op.create_index(op.f("ix_llm_usage_events_conversation_id"), "llm_usage_events", ["conversation_id"], unique=False)
    op.create_index(op.f("ix_llm_usage_events_message_id"), "llm_usage_events", ["message_id"], unique=False)
    op.create_index(op.f("ix_llm_usage_events_request_id"), "llm_usage_events", ["request_id"], unique=False)

    connection = op.get_bind()
    legacy_rows = connection.execute(
        sa.text("SELECT id, name, base_url, api_key, model, is_active, is_fallback, created_by FROM llm_provider_configs")
    ).mappings()
    for row in legacy_rows:
        provider_name = str(row["name"] or "provider").strip().lower().replace(" ", "-")
        model_key = provider_name.replace("_", "-") or f"provider-{row['id']}"
        is_minimax = "minimax" in provider_name or "MiniMax" in str(row["model"])
        account_result = connection.execute(
            sa.text(
                "INSERT INTO llm_provider_accounts "
                "(provider_name, display_name, base_url, api_key, account_billing_type, is_enabled, created_by, created_at, updated_at) "
                "VALUES (:provider_name, :display_name, :base_url, :api_key, :billing, 1, :created_by, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {
                "provider_name": provider_name,
                "display_name": row["name"],
                "base_url": row["base_url"],
                "api_key": row["api_key"],
                "billing": "token_plan" if is_minimax else "pay_as_you_go",
                "created_by": row["created_by"],
            },
        )
        account_id = account_result.lastrowid
        model_result = connection.execute(
            sa.text(
                "INSERT INTO llm_model_configs "
                "(model_key, display_name, description, provider_account_id, provider_model, capability_text, capability_vision, "
                "is_enabled, is_primary, is_fallback, sort_order, created_by, created_at, updated_at) "
                "VALUES (:model_key, :display_name, '', :account_id, :provider_model, 1, 0, 1, :is_primary, :is_fallback, :sort_order, :created_by, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {
                "model_key": model_key,
                "display_name": row["model"],
                "account_id": account_id,
                "provider_model": row["model"],
                "is_primary": row["is_active"],
                "is_fallback": row["is_fallback"],
                "sort_order": 10 if row["is_active"] else 100,
                "created_by": row["created_by"],
            },
        )
        connection.execute(
            sa.text(
                "INSERT INTO llm_quota_policies "
                "(model_config_id, billing_mode, user_daily_request_limit, user_daily_token_limit, max_completion_tokens, "
                "count_cache_hit, fail_closed_on_store_error, created_at, updated_at) "
                "VALUES (:model_id, :billing_mode, :request_limit, :token_limit, :max_completion_tokens, 0, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {
                "model_id": model_result.lastrowid,
                "billing_mode": "request_count" if is_minimax else "token_usage",
                "request_limit": 20 if is_minimax else None,
                "token_limit": None if is_minimax else 50000,
                "max_completion_tokens": None if is_minimax else 1024,
            },
        )


def downgrade() -> None:
    op.drop_index(op.f("ix_llm_usage_events_request_id"), table_name="llm_usage_events")
    op.drop_index(op.f("ix_llm_usage_events_message_id"), table_name="llm_usage_events")
    op.drop_index(op.f("ix_llm_usage_events_conversation_id"), table_name="llm_usage_events")
    op.drop_index(op.f("ix_llm_usage_events_user_id"), table_name="llm_usage_events")
    op.drop_table("llm_usage_events")
    op.drop_table("llm_quota_policies")
    op.drop_index(op.f("ix_llm_model_configs_is_fallback"), table_name="llm_model_configs")
    op.drop_index(op.f("ix_llm_model_configs_is_primary"), table_name="llm_model_configs")
    op.drop_index(op.f("ix_llm_model_configs_is_enabled"), table_name="llm_model_configs")
    op.drop_index(op.f("ix_llm_model_configs_model_key"), table_name="llm_model_configs")
    op.drop_table("llm_model_configs")
    op.drop_index(op.f("ix_llm_provider_accounts_is_enabled"), table_name="llm_provider_accounts")
    op.drop_index(op.f("ix_llm_provider_accounts_provider_name"), table_name="llm_provider_accounts")
    op.drop_table("llm_provider_accounts")
