"""Keep external campus identities linked to stable StudyAgent account IDs."""
from alembic import op
import sqlalchemy as sa

revision = "20261123_0026"
down_revision = "20261112_0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("campus_identities"):
        return
    op.create_table(
        "campus_identities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("school_id", sa.String(128), nullable=False),
        sa.Column("external_id", sa.String(128)),
        sa.Column("number", sa.String(64), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.UniqueConstraint("school_id", "external_id"),
        sa.UniqueConstraint("school_id", "number"),
        sa.UniqueConstraint("school_id", "user_id"),
    )


def downgrade() -> None:
    op.drop_table("campus_identities")
