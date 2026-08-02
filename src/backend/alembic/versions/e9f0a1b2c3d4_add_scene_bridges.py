"""Add persistent scene bridges between structured scenes.

Revision ID: e9f0a1b2c3d4
Revises: d8e9f0a1b2c3
Create Date: 2026-08-02
"""

from alembic import op
import sqlalchemy as sa


revision = "e9f0a1b2c3d4"
down_revision = "d8e9f0a1b2c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scene_bridges",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("campaign_id", sa.String(length=36), nullable=False),
        sa.Column("transition_id", sa.String(length=36), nullable=False),
        sa.Column("source_scene_id", sa.String(length=36), nullable=True),
        sa.Column("target_scene_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("previous_scene_summary", sa.Text(), nullable=False),
        sa.Column("carried_goals", sa.Text(), nullable=False),
        sa.Column("unresolved_threads", sa.Text(), nullable=False),
        sa.Column("departed_participant_ids", sa.Text(), nullable=False),
        sa.Column("departed_participant_names", sa.Text(), nullable=False),
        sa.Column("carried_participant_ids", sa.Text(), nullable=False),
        sa.Column("carried_participant_names", sa.Text(), nullable=False),
        sa.Column("negative_placement_facts", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("applied_at", sa.DateTime(), nullable=True),
        sa.Column("undone_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["campaigns.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["transition_id"],
            ["scene_transitions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_scene_id"],
            ["scenes.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["target_scene_id"],
            ["scenes.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "transition_id",
            name="uq_scene_bridge_transition",
        ),
    )
    op.create_index(
        op.f("ix_scene_bridges_campaign_id"),
        "scene_bridges",
        ["campaign_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_scene_bridges_target_scene_id"),
        "scene_bridges",
        ["target_scene_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_scene_bridges_target_scene_id"),
        table_name="scene_bridges",
    )
    op.drop_index(
        op.f("ix_scene_bridges_campaign_id"),
        table_name="scene_bridges",
    )
    op.drop_table("scene_bridges")
