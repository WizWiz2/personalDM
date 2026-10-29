"""Durable journal of prepare-phase world changes for crash-safe compensation."""
import sqlalchemy as sa

from alembic import op

from app.db.migration_compat import adopt_existing_table

revision = "b9e1c2d3f4a5"
down_revision = "f4a5b6c7d8e9"
branch_labels = None
depends_on = None


def upgrade():
    if adopt_existing_table(
        "provisional_turn_effects",
        required_columns={"id", "trigger_turn_id", "kind", "entity_id", "scene_id", "previous", "status"},
        primary_key={"id"},
        non_nullable={"id", "trigger_turn_id", "kind", "entity_id", "status"},
    ):
        return
    op.create_table(
        "provisional_turn_effects",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("trigger_turn_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("entity_id", sa.String(length=36), nullable=False),
        sa.Column("scene_id", sa.String(length=36), nullable=True),
        sa.Column("previous", sa.String(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="provisional"),
    )
    op.create_index("ix_provisional_turn_effects_trigger_turn_id", "provisional_turn_effects", ["trigger_turn_id"])


def downgrade():
    op.drop_index("ix_provisional_turn_effects_trigger_turn_id", "provisional_turn_effects")
    op.drop_table("provisional_turn_effects")
