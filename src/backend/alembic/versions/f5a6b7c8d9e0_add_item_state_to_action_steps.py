"""Persist item state snapshots for typed action-sequence inventory steps."""

from alembic import op
import sqlalchemy as sa


revision = "f5a6b7c8d9e0"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = {column["name"] for column in inspector.get_columns("action_steps")}
    for name in (
        "item_id",
        "item_name",
        "item_operation",
        "item_previous_owner_id",
        "item_previous_location_id",
        "item_result_owner_id",
        "item_result_location_id",
    ):
        if name not in existing:
            op.add_column("action_steps", sa.Column(name, sa.String(length=36), nullable=True))


def downgrade() -> None:
    for name in (
        "item_result_location_id",
        "item_result_owner_id",
        "item_previous_location_id",
        "item_previous_owner_id",
        "item_id",
        "item_operation",
        "item_name",
    ):
        op.drop_column("action_steps", name)
