"""Persist human-readable item labels for the narration authority contract."""

from alembic import op
import sqlalchemy as sa


revision = "f6a7b8c9d0e1"
down_revision = "f5a6b7c8d9e0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = {column["name"] for column in inspector.get_columns("action_steps")}
    for name in ("item_name", "item_operation"):
        if name not in existing:
            op.add_column("action_steps", sa.Column(name, sa.String(length=255 if name == "item_name" else 32), nullable=True))


def downgrade() -> None:
    op.drop_column("action_steps", "item_operation")
    op.drop_column("action_steps", "item_name")
