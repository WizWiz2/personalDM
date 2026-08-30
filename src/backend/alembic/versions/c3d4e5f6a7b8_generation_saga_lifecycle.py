"""Add durable narrative generation saga lifecycle.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-08-29
"""

from alembic import op
import sqlalchemy as sa

from app.db.migration_compat import adopt_existing_table


revision = "c3d4e5f6a7b8"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None

_TABLE = "generation_lifecycles"


def upgrade() -> None:
    adopted = adopt_existing_table(
        _TABLE,
        required_columns={
            "generation_run_id",
            "phase",
            "attempt",
            "received_at",
            "planned_at",
            "prepared_at",
            "narrated_at",
            "published_at",
            "post_turn_done_at",
            "compensated_at",
            "updated_at",
        },
        primary_key={"generation_run_id"},
        non_nullable={"generation_run_id", "phase", "attempt", "updated_at"},
        foreign_keys={
            (("generation_run_id",), "generation_runs", ("id",)),
        },
    )
    if not adopted:
        op.create_table(
            _TABLE,
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
                ["generation_run_id"], ["generation_runs.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("generation_run_id"),
        )


def downgrade() -> None:
    op.drop_table(_TABLE)
