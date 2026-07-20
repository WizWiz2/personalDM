"""Add player role, persistent generation runs and post-turn jobs.

Revision ID: c1a2b3c4d5e6
Revises: 7f4b8d2c91aa
Create Date: 2026-07-19
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "c1a2b3c4d5e6"
down_revision: str | None = "7f4b8d2c91aa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("campaigns") as batch_op:
        batch_op.add_column(
            sa.Column("player_character_id", sa.String(length=36), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_campaign_player_character",
            "entities",
            ["player_character_id"],
            ["id"],
            ondelete="SET NULL",
        )

    op.create_table(
        "generation_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("campaign_id", sa.String(length=36), nullable=False),
        sa.Column("user_turn_id", sa.String(length=36), nullable=False),
        sa.Column("assistant_turn_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="running"),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["assistant_turn_id"], ["turns.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_turn_id"], ["turns.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_turn_id"),
    )
    op.create_index("ix_generation_runs_campaign_status", "generation_runs", ["campaign_id", "status"])

    op.create_table(
        "post_turn_jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("campaign_id", sa.String(length=36), nullable=False),
        sa.Column("assistant_turn_id", sa.String(length=36), nullable=False),
        sa.Column("job_type", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("locked_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["assistant_turn_id"], ["turns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("assistant_turn_id", "job_type", name="uq_post_turn_job"),
    )
    op.create_index("ix_post_turn_jobs_status", "post_turn_jobs", ["status", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_post_turn_jobs_status", table_name="post_turn_jobs")
    op.drop_table("post_turn_jobs")
    op.drop_index("ix_generation_runs_campaign_status", table_name="generation_runs")
    op.drop_table("generation_runs")

    with op.batch_alter_table("campaigns") as batch_op:
        batch_op.drop_constraint("fk_campaign_player_character", type_="foreignkey")
        batch_op.drop_column("player_character_id")
