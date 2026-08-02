"""Add thesis lifecycle profiles.

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-08-02
"""

from alembic import op
import sqlalchemy as sa


revision = "b2c3d4e5f6a7"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


_TTL_CASE = """
CASE thesis_type
    WHEN 'canon' THEN 16
    WHEN 'intention' THEN 8
    WHEN 'relationship_dynamic' THEN 10
    WHEN 'secret' THEN 12
    WHEN 'tension' THEN 5
    WHEN 'unresolved_beat' THEN 8
    WHEN 'visual_state' THEN 3
    WHEN 'music_mood' THEN 3
    ELSE 8
END
"""


def upgrade() -> None:
    op.create_table(
        "thesis_lifecycle_profiles",
        sa.Column("thesis_id", sa.String(length=36), nullable=False),
        sa.Column("semantic_key", sa.String(length=160), nullable=False),
        sa.Column("ttl_turns", sa.Integer(), nullable=False),
        sa.Column("last_reinforced_turn_id", sa.String(length=36), nullable=True),
        sa.Column("closure_reason", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "ttl_turns >= 1 AND ttl_turns <= 50",
            name="ck_thesis_lifecycle_ttl",
        ),
        sa.ForeignKeyConstraint(
            ["thesis_id"],
            ["scene_theses.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["last_reinforced_turn_id"],
            ["turns.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("thesis_id"),
    )
    op.create_index(
        op.f("ix_thesis_lifecycle_profiles_last_reinforced_turn_id"),
        "thesis_lifecycle_profiles",
        ["last_reinforced_turn_id"],
        unique=False,
    )
    op.execute(
        sa.text(
            f"""
            INSERT INTO thesis_lifecycle_profiles (
                thesis_id,
                semantic_key,
                ttl_turns,
                last_reinforced_turn_id,
                closure_reason,
                created_at,
                updated_at
            )
            SELECT
                id,
                substr(lower(trim(text)), 1, 160),
                {_TTL_CASE},
                source_turn_id,
                CASE WHEN status = 'active' THEN NULL ELSE status END,
                CURRENT_TIMESTAMP,
                CURRENT_TIMESTAMP
            FROM scene_theses
            """
        )
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_thesis_lifecycle_profiles_last_reinforced_turn_id"),
        table_name="thesis_lifecycle_profiles",
    )
    op.drop_table("thesis_lifecycle_profiles")
