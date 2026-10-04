"""Typed witnesses of facts and beliefs."""

import sqlalchemy as sa
from alembic import op

revision = "f3a4b5c6d7e8"
down_revision = "e2f3a4b5c6d7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table("memory_witnesses"):
        op.create_table(
            "memory_witnesses",
            sa.Column("memory_id", sa.String(length=36), primary_key=True),
            sa.Column("entity_id", sa.String(length=36), primary_key=True),
        )


def downgrade() -> None:
    op.drop_table("memory_witnesses")
