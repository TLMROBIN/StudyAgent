"""Encrypt provider credentials without changing model routing or key values."""
from alembic import op
import sqlalchemy as sa

revision = "20261023_0021"
down_revision = "20260716_0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from backend.secret_storage import encrypt_legacy_provider_keys
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        for table in ("llm_provider_accounts", "llm_provider_configs"):
            if sa.inspect(bind).has_table(table):
                op.alter_column(table, "api_key", existing_type=sa.String(512), type_=sa.Text())
    encrypt_legacy_provider_keys(bind)


def downgrade() -> None:
    # An operator explicitly invoking downgrade requests compatibility with the
    # pre-encryption application. Retain the key until this transaction completes.
    from backend.secret_storage import decrypt_secret
    bind = op.get_bind()
    for table in ("llm_provider_accounts", "llm_provider_configs"):
        if not sa.inspect(bind).has_table(table):
            continue
        rows = bind.execute(sa.text(f"SELECT id, api_key FROM {table}")).mappings().all()
        restored = [(row["id"], decrypt_secret(row["api_key"])) for row in rows]
        for row_id, value in restored:
            bind.execute(sa.text(f"UPDATE {table} SET api_key=:value WHERE id=:id"), {"value": value, "id": row_id})
        if bind.dialect.name != "sqlite":
            op.alter_column(table, "api_key", existing_type=sa.Text(), type_=sa.String(512))
