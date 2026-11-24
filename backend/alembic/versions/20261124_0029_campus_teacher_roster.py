"""Allow campus teacher accounts only for the approved teacher roster."""
from alembic import op
import sqlalchemy as sa

revision = "20261124_0029"
down_revision = "20261124_0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("campus_roster_teachers"):
        return
    op.create_table(
        "campus_roster_teachers",
        sa.Column("school_id", sa.String(128), primary_key=True),
        sa.Column("number", sa.String(64), primary_key=True),
        sa.Column("full_name", sa.String(64), nullable=False),
        sa.Column("username", sa.String(64), nullable=False),
        sa.UniqueConstraint("school_id", "username"),
    )


def downgrade() -> None:
    op.drop_table("campus_roster_teachers")
