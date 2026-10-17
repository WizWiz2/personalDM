"""Canonical item placement detail and durable aliases of introduced NPCs.

Revision ID: a9d0e1f2a3b4
Revises: a8c9d0e1f2a3
"""
from alembic import op
import sqlalchemy as sa

revision = "a9d0e1f2a3b4"
down_revision = "a8c9d0e1f2a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    item_columns = {column["name"] for column in inspector.get_columns("items")}
    if "placement_detail" not in item_columns:
        op.add_column("items", sa.Column("placement_detail", sa.Text(), nullable=True))
    step_columns = {column["name"] for column in inspector.get_columns("action_steps")}
    for name in ("item_previous_placement_detail", "item_result_placement_detail"):
        if name not in step_columns:
            op.add_column("action_steps", sa.Column(name, sa.Text(), nullable=True))
    if "npc_identity_anchors" not in inspector.get_table_names():
        op.create_table(
            "npc_identity_anchors",
            sa.Column("campaign_id", sa.String(36), sa.ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False),
            sa.Column("identity_key", sa.String(250), nullable=False),
            sa.Column("entity_id", sa.String(36), nullable=False),
            sa.PrimaryKeyConstraint("campaign_id", "identity_key"),
        )


def downgrade() -> None:
    op.drop_table("npc_identity_anchors")
    op.drop_column("action_steps", "item_result_placement_detail")
    op.drop_column("action_steps", "item_previous_placement_detail")
    op.drop_column("items", "placement_detail")
