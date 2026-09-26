"""Separate player-facing blockers from internal execution diagnostics."""

from alembic import op
import sqlalchemy as sa

revision = "a8c9d0e1f2a3"
down_revision = "a7b8c9d0e1f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("action_steps")}
    if "public_blocking_reason" not in columns:
        op.add_column("action_steps", sa.Column("public_blocking_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("action_steps", "public_blocking_reason")
