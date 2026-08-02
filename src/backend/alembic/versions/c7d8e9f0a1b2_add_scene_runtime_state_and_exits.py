"""Add authoritative scene runtime state and concrete location exits.

Revision ID: c7d8e9f0a1b2
Revises: b6f7c8d9e0a1
Create Date: 2026-08-02
"""

from alembic import op
import sqlalchemy as sa


revision = "c7d8e9f0a1b2"
down_revision = "b6f7c8d9e0a1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scene_runtime_states",
        sa.Column("scene_id", sa.String(length=36), nullable=False),
        sa.Column("world_time_label", sa.String(length=255), nullable=True),
        sa.Column("world_time_order", sa.Integer(), nullable=False),
        sa.Column("scene_goal", sa.Text(), nullable=True),
        sa.Column("active_conflict", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["scene_id"], ["scenes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("scene_id"),
    )
    op.create_table(
        "location_exits",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("campaign_id", sa.String(length=36), nullable=False),
        sa.Column("from_location_id", sa.String(length=36), nullable=False),
        sa.Column("to_location_id", sa.String(length=36), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("direction", sa.String(length=100), nullable=True),
        sa.Column("travel_time", sa.String(length=100), nullable=True),
        sa.Column("access_rule", sa.Text(), nullable=True),
        sa.Column("discovered", sa.Boolean(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["campaign_id"], ["campaigns.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["from_location_id"], ["entities.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["to_location_id"], ["entities.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "from_location_id",
            "to_location_id",
            name="uq_location_exit_direction",
        ),
    )

    op.execute(
        sa.text(
            """
            INSERT INTO scene_runtime_states (
                scene_id,
                world_time_label,
                world_time_order,
                scene_goal,
                active_conflict,
                created_at,
                updated_at
            )
            SELECT
                scenes.id,
                NULL,
                ROW_NUMBER() OVER (
                    PARTITION BY scenes.campaign_id ORDER BY scenes.created_at, scenes.id
                ) - 1,
                NULL,
                NULL,
                CURRENT_TIMESTAMP,
                CURRENT_TIMESTAMP
            FROM scenes
            """
        )
    )


def downgrade() -> None:
    op.drop_table("location_exits")
    op.drop_table("scene_runtime_states")
