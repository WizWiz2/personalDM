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
            op.add_column(
                "action_steps",
                sa.Column(
                    name,
                    sa.String(length=255 if name == "item_name" else 32),
                    nullable=True,
                ),
            )


def downgrade() -> None:
    # The f5 schema already owns item_name and item_operation. In current migration
    # history f6 is therefore an idempotent compatibility migration; downgrading to
    # f5 must preserve those columns so f5 can remove them when it is downgraded.
    pass
