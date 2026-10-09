"""Persist action performers separately from response speakers."""
from alembic import op
import sqlalchemy as sa

revision = "b9d0e1f2a3b4"
down_revision = "a8c9d0e1f2a3"
branch_labels = None
depends_on = None


def upgrade():
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("action_steps")}
    for name, length in (("actor_id", 36), ("actor_name", 120)):
        if name not in existing:
            op.add_column("action_steps", sa.Column(name, sa.String(length), nullable=True))


def downgrade():
    op.drop_column("action_steps", "actor_name")
    op.drop_column("action_steps", "actor_id")
