"""Executed physical item properties, independent of narrative summaries."""
from alembic import op
import sqlalchemy as sa

from app.db.migration_compat import adopt_existing_table

revision = "c1f2a3b4c5d6"
down_revision = "b0e1f2a3b4c5"
branch_labels = None
depends_on = None


def upgrade():
    if "source_action_index" not in {row["name"] for row in sa.inspect(op.get_bind()).get_columns("action_steps")}:
        op.add_column("action_steps", sa.Column("source_action_index", sa.Integer(), nullable=True))
    if not adopt_existing_table(
        "item_property_effects", required_columns=("source_step_id", "item_id", "property_name", "value"),
        primary_key=("source_step_id", "item_id", "property_name"),
        foreign_keys=((("source_step_id",), "action_steps", ("id",)),
                      (("item_id",), "items", ("entity_id",))),
        non_nullable=("source_step_id", "item_id", "property_name", "value"),
    ):
        op.create_table(
            "item_property_effects",
            sa.Column("source_step_id", sa.String(36), sa.ForeignKey("action_steps.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("item_id", sa.String(36), sa.ForeignKey("items.entity_id", ondelete="CASCADE"), primary_key=True),
            sa.Column("property_name", sa.String(120), primary_key=True),
            sa.Column("value", sa.Text(), nullable=False),
        )
    indexes = {row["name"] for row in sa.inspect(op.get_bind()).get_indexes("item_property_effects")}
    if "ix_item_property_effects_item_id" not in indexes:
        op.create_index("ix_item_property_effects_item_id", "item_property_effects", ["item_id"])
    if not adopt_existing_table(
        "item_creation_effects", required_columns=("source_step_id", "item_id", "owner_id", "location_id", "placement_detail"),
        primary_key=("source_step_id", "item_id"),
        foreign_keys=((("source_step_id",), "action_steps", ("id",)),
                      (("item_id",), "items", ("entity_id",))),
        non_nullable=("source_step_id", "item_id"),
    ):
        op.create_table(
            "item_creation_effects",
            sa.Column("source_step_id", sa.String(36), sa.ForeignKey("action_steps.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("item_id", sa.String(36), sa.ForeignKey("items.entity_id", ondelete="CASCADE"), primary_key=True),
            sa.Column("owner_id", sa.String(36), nullable=True),
            sa.Column("location_id", sa.String(36), nullable=True),
            sa.Column("placement_detail", sa.Text(), nullable=True),
        )
    indexes = {row["name"] for row in sa.inspect(op.get_bind()).get_indexes("item_creation_effects")}
    if "ix_item_creation_effects_item_id" not in indexes:
        op.create_index("ix_item_creation_effects_item_id", "item_creation_effects", ["item_id"])


def downgrade():
    op.drop_table("item_creation_effects")
    op.drop_table("item_property_effects")
    op.drop_column("action_steps", "source_action_index")
