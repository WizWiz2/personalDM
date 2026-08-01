"""Add typed scene transition audit records.

Revision ID: a5e6f7b8c9d0
Revises: f4d5e6a7b8c9
Create Date: 2026-08-01
"""

from alembic import op
import sqlalchemy as sa


revision = "a5e6f7b8c9d0"
down_revision = "f4d5e6a7b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scene_transitions",
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
    op.create_index(
        "ix_scene_transitions_campaign_id",
        "scene_transitions",
        ["campaign_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_scene_transitions_campaign_id",
        table_name="scene_transitions",
    )
    op.drop_table("scene_transitions")
