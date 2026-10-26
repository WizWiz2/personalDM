"""Durable item content bound to executed source steps.

Revision ID: b0e1f2a3b4c5
Revises: a9d0e1f2a3b4
"""
from alembic import op
import sqlalchemy as sa

from app.db.migration_compat import adopt_existing_table

revision = "b0e1f2a3b4c5"
down_revision = "a9d0e1f2a3b4"
branch_labels = None
depends_on = None


def upgrade():
    if not adopt_existing_table(
        "item_content_disclosures", required_columns=("source_step_id", "item_id", "content"),
        primary_key=("source_step_id", "item_id"),
        foreign_keys=((('source_step_id',), 'action_steps', ('id',)),
                      (('item_id',), 'items', ('entity_id',))),
        non_nullable=("source_step_id", "item_id", "content"),
    ):
        op.create_table(
            "item_content_disclosures",
            sa.Column("source_step_id", sa.String(36), sa.ForeignKey("action_steps.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("item_id", sa.String(36), sa.ForeignKey("items.entity_id", ondelete="CASCADE"), primary_key=True),
            sa.Column("content", sa.Text(), nullable=False),
        )
    indexes = {index['name'] for index in sa.inspect(op.get_bind()).get_indexes('item_content_disclosures')}
    if "ix_item_content_disclosures_item_id" not in indexes:
        op.create_index("ix_item_content_disclosures_item_id", "item_content_disclosures", ["item_id"])


def downgrade():
    op.drop_table("item_content_disclosures")
