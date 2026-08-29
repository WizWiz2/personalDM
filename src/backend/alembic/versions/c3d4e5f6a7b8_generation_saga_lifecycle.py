"""Add durable narrative generation saga lifecycle.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-08-29
"""

from alembic import op
import sqlalchemy as sa


revision = "c3d4e5f6a7b8"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "generation_lifecycles",
        sa.Column("generation_run_id", sa.String(length=36), nullable=False),
        sa.Column("phase", sa.String(length=50), nullable=False, server_default="received"),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("received_at", sa.DateTime(), nullable=True),
        sa.Column("planned_at", sa.DateTime(), nullable=True),
        sa.Column("prepared_at", sa.DateTime(), nullable=True),
        sa.Column("narrated_at", sa.DateTime(), nullable=True),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("post_turn_done_at", sa.DateTime(), nullable=True),
        sa.Column("compensated_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["generation_run_id"],
            ["generation_runs.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("generation_run_id"),
    )


def downgrade() -> None:
    op.drop_table("generation_lifecycles")
