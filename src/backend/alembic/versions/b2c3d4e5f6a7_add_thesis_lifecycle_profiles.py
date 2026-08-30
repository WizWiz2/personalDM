"""Add thesis lifecycle profiles.

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-08-02
"""

from alembic import op
import sqlalchemy as sa

from app.db.migration_compat import adopt_existing_table, ensure_index


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

_TABLE = "thesis_lifecycle_profiles"


def upgrade() -> None:
    adopted = adopt_existing_table(
        _TABLE,
        required_columns={
            "thesis_id",
            "semantic_key",
            "ttl_turns",
            "last_reinforced_turn_id",
            "closure_reason",
            "created_at",
            "updated_at",
        },
        primary_key={"thesis_id"},
        non_nullable={
            "thesis_id",
            "semantic_key",
            "ttl_turns",
            "created_at",
            "updated_at",
        },
        foreign_keys={
            (("thesis_id",), "scene_theses", ("id",)),
            (("last_reinforced_turn_id",), "turns", ("id",)),
        },
    )
    if not adopted:
        op.create_table(
            _TABLE,
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
                ["thesis_id"], ["scene_theses.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["last_reinforced_turn_id"], ["turns.id"], ondelete="SET NULL"
            ),
            sa.PrimaryKeyConstraint("thesis_id"),
        )

    ensure_index(
        _TABLE,
        "ix_thesis_lifecycle_profiles_last_reinforced_turn_id",
        ["last_reinforced_turn_id"],
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
                scene_theses.id,
                substr(lower(trim(scene_theses.text)), 1, 160),
                {_TTL_CASE},
                scene_theses.source_turn_id,
                CASE WHEN scene_theses.status = 'active' THEN NULL ELSE scene_theses.status END,
                CURRENT_TIMESTAMP,
                CURRENT_TIMESTAMP
            FROM scene_theses
            WHERE NOT EXISTS (
                SELECT 1
                FROM thesis_lifecycle_profiles existing
                WHERE existing.thesis_id = scene_theses.id
            )
            """
        )
    )


def downgrade() -> None:
    op.drop_index(
        "ix_thesis_lifecycle_profiles_last_reinforced_turn_id",
        table_name=_TABLE,
    )
    op.drop_table(_TABLE)
