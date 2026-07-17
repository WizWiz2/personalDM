"""Add turn UUID provenance and acting-character audit fields.

Revision ID: 7f4b8d2c91aa
Revises: 293b3517b6ac
Create Date: 2026-07-17
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "7f4b8d2c91aa"
down_revision: str | None = "293b3517b6ac"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SOURCE_TABLES = (
    "scene_theses",
    "character_goals",
    "facts",
    "beliefs",
    "relationship_assertions",
)


def upgrade() -> None:
    # Legacy source_turn_id columns were INTEGER while turns.id is UUID text.
    # Existing prototype databases did not contain usable values, so clear them
    # before rebuilding the columns with real foreign keys.
    for table_name in _SOURCE_TABLES:
        op.execute(f"UPDATE {table_name} SET source_turn_id = NULL")

    with op.batch_alter_table("campaigns") as batch_op:
        batch_op.create_foreign_key(
            "fk_campaign_current_scene",
            "scenes",
            ["current_scene_id"],
            ["id"],
            ondelete="SET NULL",
        )

    with op.batch_alter_table("turns") as batch_op:
        batch_op.add_column(
            sa.Column("acting_character_id", sa.String(length=36), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_turn_acting_character",
            "entities",
            ["acting_character_id"],
            ["id"],
            ondelete="SET NULL",
        )

    for table_name in _SOURCE_TABLES:
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.alter_column(
                "source_turn_id",
                existing_type=sa.Integer(),
                type_=sa.String(length=36),
                existing_nullable=True,
            )
            batch_op.create_foreign_key(
                f"fk_{table_name}_source_turn",
                "turns",
                ["source_turn_id"],
                ["id"],
                ondelete="SET NULL",
            )

    with op.batch_alter_table("items") as batch_op:
        batch_op.create_check_constraint(
            "ck_item_single_position",
            "NOT (current_owner_id IS NOT NULL AND current_location_id IS NOT NULL)",
        )


def downgrade() -> None:
    with op.batch_alter_table("items") as batch_op:
        batch_op.drop_constraint("ck_item_single_position", type_="check")

    for table_name in reversed(_SOURCE_TABLES):
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.drop_constraint(
                f"fk_{table_name}_source_turn",
                type_="foreignkey",
            )
            batch_op.alter_column(
                "source_turn_id",
                existing_type=sa.String(length=36),
                type_=sa.Integer(),
                existing_nullable=True,
            )

    with op.batch_alter_table("turns") as batch_op:
        batch_op.drop_constraint("fk_turn_acting_character", type_="foreignkey")
        batch_op.drop_column("acting_character_id")

    with op.batch_alter_table("campaigns") as batch_op:
        batch_op.drop_constraint("fk_campaign_current_scene", type_="foreignkey")
