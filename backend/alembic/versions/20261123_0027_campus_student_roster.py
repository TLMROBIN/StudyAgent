"""Allow campus first-time student accounts only for the approved roster."""
from alembic import op
import sqlalchemy as sa

revision = "20261123_0027"
down_revision = "20261123_0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("campus_roster_students"):
        return
    op.create_table(
        "campus_roster_students",
        sa.Column("school_id", sa.String(128), primary_key=True),
        sa.Column("number", sa.String(64), primary_key=True),
        sa.Column("full_name", sa.String(64), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("campus_roster_students")
