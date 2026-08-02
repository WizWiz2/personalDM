"""Add durable compound action sequences and ordered steps.

Revision ID: d8e9f0a1b2c3
Revises: c7d8e9f0a1b2
Create Date: 2026-08-02
"""

from alembic import op
import sqlalchemy as sa


revision = "d8e9f0a1b2c3"
down_revision = "c7d8e9f0a1b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "action_sequences",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("campaign_id", sa.String(length=36), nullable=False),
        sa.Column("trigger_turn_id", sa.String(length=36), nullable=False),
        sa.Column("source_scene_id", sa.String(length=36), nullable=True),
        sa.Column("final_scene_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("planned_steps", sa.Integer(), nullable=False),
        sa.Column("completed_steps", sa.Integer(), nullable=False),
        sa.Column("blocked_step_index", sa.Integer(), nullable=True),
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
            ["trigger_turn_id"],
            ["turns.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_scene_id"],
            ["scenes.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["final_scene_id"],
            ["scenes.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "trigger_turn_id",
            name="uq_action_sequence_trigger_turn",
        ),
    )
    op.create_index(
        op.f("ix_action_sequences_campaign_id"),
        "action_sequences",
        ["campaign_id"],
        unique=False,
    )

    op.create_table(
        "action_steps",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("sequence_id", sa.String(length=36), nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("action_type", sa.String(length=50), nullable=False),
        sa.Column("intent", sa.Text(), nullable=False),
        sa.Column("resolution", sa.String(length=50), nullable=False),
        sa.Column("safe_mundane", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("observable_outcome", sa.Text(), nullable=True),
        sa.Column("blocking_reason", sa.Text(), nullable=True),
        sa.Column("transition_id", sa.String(length=36), nullable=True),
        sa.Column("source_scene_id", sa.String(length=36), nullable=True),
        sa.Column("target_scene_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["sequence_id"],
            ["action_sequences.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["transition_id"],
            ["scene_transitions.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["source_scene_id"],
            ["scenes.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["target_scene_id"],
            ["scenes.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "sequence_id",
            "step_index",
            name="uq_action_step_sequence_index",
        ),
    )
    op.create_index(
        op.f("ix_action_steps_sequence_id"),
        "action_steps",
        ["sequence_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_action_steps_sequence_id"), table_name="action_steps")
    op.drop_table("action_steps")
    op.drop_index(
        op.f("ix_action_sequences_campaign_id"),
        table_name="action_sequences",
    )
    op.drop_table("action_sequences")
