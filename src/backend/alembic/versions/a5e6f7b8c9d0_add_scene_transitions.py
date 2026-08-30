"""Add typed scene transition audit records.

Revision ID: a5e6f7b8c9d0
Revises: f4d5e6a7b8c9
Create Date: 2026-08-01
"""

from alembic import op
import sqlalchemy as sa

from app.db.migration_compat import adopt_existing_table, ensure_index


revision = "a5e6f7b8c9d0"
down_revision = "f4d5e6a7b8c9"
branch_labels = None
depends_on = None

_TABLE = "scene_transitions"
_INDEX = "ix_scene_transitions_campaign_id"


def upgrade() -> None:
    adopted = adopt_existing_table(
        _TABLE,
        required_columns={
            "id",
            "campaign_id",
            "source_scene_id",
            "target_scene_id",
            "trigger_turn_id",
            "transition_type",
            "status",
            "source_location_id",
            "target_location_id",
            "elapsed_time",
            "time_after",
            "reason",
            "detector",
            "created_at",
            "undone_at",
        },
        primary_key={"id"},
        non_nullable={
            "id",
            "campaign_id",
            "target_scene_id",
            "transition_type",
            "status",
            "detector",
            "created_at",
        },
        foreign_keys={
            (("campaign_id",), "campaigns", ("id",)),
            (("source_scene_id",), "scenes", ("id",)),
            (("target_scene_id",), "scenes", ("id",)),
            (("trigger_turn_id",), "turns", ("id",)),
            (("source_location_id",), "entities", ("id",)),
            (("target_location_id",), "entities", ("id",)),
        },
    )

    if not adopted:
        op.create_table(
            _TABLE,
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("campaign_id", sa.String(length=36), nullable=False),
            sa.Column("source_scene_id", sa.String(length=36), nullable=True),
            sa.Column("target_scene_id", sa.String(length=36), nullable=False),
            sa.Column("trigger_turn_id", sa.String(length=36), nullable=True),
            sa.Column("transition_type", sa.String(length=50), nullable=False),
            sa.Column("status", sa.String(length=50), nullable=False),
            sa.Column("source_location_id", sa.String(length=36), nullable=True),
            sa.Column("target_location_id", sa.String(length=36), nullable=True),
            sa.Column("elapsed_time", sa.String(length=255), nullable=True),
            sa.Column("time_after", sa.String(length=255), nullable=True),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("detector", sa.String(length=100), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("undone_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(
                ["campaign_id"], ["campaigns.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["source_scene_id"], ["scenes.id"], ondelete="SET NULL"
            ),
            sa.ForeignKeyConstraint(
                ["target_scene_id"], ["scenes.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["trigger_turn_id"], ["turns.id"], ondelete="SET NULL"
            ),
            sa.ForeignKeyConstraint(
                ["source_location_id"], ["entities.id"], ondelete="SET NULL"
            ),
            sa.ForeignKeyConstraint(
                ["target_location_id"], ["entities.id"], ondelete="SET NULL"
            ),
            sa.PrimaryKeyConstraint("id"),
        )

    ensure_index(_TABLE, _INDEX, ["campaign_id"])


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE)
    op.drop_table(_TABLE)
