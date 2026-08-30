"""Add durable post-generation narration validation records.

Revision ID: f0a1b2c3d4e5
Revises: e9f0a1b2c3d4
Create Date: 2026-08-02
"""

from alembic import op
import sqlalchemy as sa

from app.db.migration_compat import adopt_existing_table, ensure_index


revision = "f0a1b2c3d4e5"
down_revision = "e9f0a1b2c3d4"
branch_labels = None
depends_on = None

_TABLE = "narration_validation_runs"


def upgrade() -> None:
    adopted = adopt_existing_table(
        _TABLE,
        required_columns={
            "id",
            "campaign_id",
            "trigger_turn_id",
            "assistant_turn_id",
            "scene_id",
            "status",
            "draft_text",
            "final_text",
            "attempts_json",
            "violation_count",
            "repair_attempts",
            "validator_model_name",
            "failure_reason",
            "created_at",
            "updated_at",
        },
        primary_key={"id"},
        non_nullable={
            "id",
            "campaign_id",
            "trigger_turn_id",
            "status",
            "draft_text",
            "attempts_json",
            "violation_count",
            "repair_attempts",
            "created_at",
            "updated_at",
        },
        foreign_keys={
            (("campaign_id",), "campaigns", ("id",)),
            (("trigger_turn_id",), "turns", ("id",)),
            (("assistant_turn_id",), "turns", ("id",)),
            (("scene_id",), "scenes", ("id",)),
        },
    )
    if not adopted:
        op.create_table(
            _TABLE,
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("campaign_id", sa.String(length=36), nullable=False),
            sa.Column("trigger_turn_id", sa.String(length=36), nullable=False),
            sa.Column("assistant_turn_id", sa.String(length=36), nullable=True),
            sa.Column("scene_id", sa.String(length=36), nullable=True),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("draft_text", sa.Text(), nullable=False),
            sa.Column("final_text", sa.Text(), nullable=True),
            sa.Column("attempts_json", sa.Text(), nullable=False),
            sa.Column("violation_count", sa.Integer(), nullable=False),
            sa.Column("repair_attempts", sa.Integer(), nullable=False),
            sa.Column("validator_model_name", sa.String(length=255), nullable=True),
            sa.Column("failure_reason", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(
                ["campaign_id"], ["campaigns.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["trigger_turn_id"], ["turns.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["assistant_turn_id"], ["turns.id"], ondelete="SET NULL"
            ),
            sa.ForeignKeyConstraint(
                ["scene_id"], ["scenes.id"], ondelete="SET NULL"
            ),
            sa.PrimaryKeyConstraint("id"),
        )

    ensure_index(_TABLE, "ix_narration_validation_runs_campaign_id", ["campaign_id"])
    ensure_index(
        _TABLE,
        "ix_narration_validation_runs_trigger_turn_id",
        ["trigger_turn_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_narration_validation_runs_trigger_turn_id", table_name=_TABLE)
    op.drop_index("ix_narration_validation_runs_campaign_id", table_name=_TABLE)
    op.drop_table(_TABLE)
