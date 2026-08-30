"""Separate durable facts from transient narrative details.

Revision ID: a1b2c3d4e5f6
Revises: f0a1b2c3d4e5
Create Date: 2026-08-02
"""

from alembic import op
import sqlalchemy as sa

from app.db.migration_compat import adopt_existing_table, ensure_index


revision = "a1b2c3d4e5f6"
down_revision = "f0a1b2c3d4e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    profiles_adopted = adopt_existing_table(
        "fact_memory_profiles",
        required_columns={
            "fact_id",
            "memory_kind",
            "subject_entity_id",
            "created_at",
            "updated_at",
        },
        primary_key={"fact_id"},
        non_nullable={"fact_id", "memory_kind", "created_at", "updated_at"},
        foreign_keys={
            (("fact_id",), "facts", ("id",)),
            (("subject_entity_id",), "entities", ("id",)),
        },
    )
    if not profiles_adopted:
        op.create_table(
            "fact_memory_profiles",
            sa.Column("fact_id", sa.String(length=36), nullable=False),
            sa.Column("memory_kind", sa.String(length=50), nullable=False),
            sa.Column("subject_entity_id", sa.String(length=36), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint(
                "memory_kind IN ('world_canon', 'entity_state', 'scene_state')",
                name="ck_fact_memory_profile_kind",
            ),
            sa.ForeignKeyConstraint(
                ["fact_id"], ["facts.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["subject_entity_id"], ["entities.id"], ondelete="SET NULL"
            ),
            sa.PrimaryKeyConstraint("fact_id"),
        )
    ensure_index(
        "fact_memory_profiles",
        "ix_fact_memory_profiles_memory_kind",
        ["memory_kind"],
    )
    ensure_index(
        "fact_memory_profiles",
        "ix_fact_memory_profiles_subject_entity_id",
        ["subject_entity_id"],
    )
    op.execute(
        sa.text(
            """
            INSERT INTO fact_memory_profiles (
                fact_id,
                memory_kind,
                subject_entity_id,
                created_at,
                updated_at
            )
            SELECT
                facts.id,
                CASE
                    WHEN facts.scope = 'scene' THEN 'scene_state'
                    ELSE 'world_canon'
                END,
                NULL,
                CURRENT_TIMESTAMP,
                CURRENT_TIMESTAMP
            FROM facts
            WHERE NOT EXISTS (
                SELECT 1
                FROM fact_memory_profiles existing
                WHERE existing.fact_id = facts.id
            )
            """
        )
    )

    details_adopted = adopt_existing_table(
        "narrative_details",
        required_columns={
            "id",
            "campaign_id",
            "scene_id",
            "source_turn_id",
            "subject_entity_id",
            "detail_type",
            "text",
            "visibility",
            "turn_window",
            "created_at",
        },
        primary_key={"id"},
        non_nullable={
            "id",
            "campaign_id",
            "scene_id",
            "detail_type",
            "text",
            "visibility",
            "turn_window",
            "created_at",
        },
        foreign_keys={
            (("campaign_id",), "campaigns", ("id",)),
            (("scene_id",), "scenes", ("id",)),
            (("source_turn_id",), "turns", ("id",)),
            (("subject_entity_id",), "entities", ("id",)),
        },
        unique_constraints={
            ("scene_id", "source_turn_id", "subject_entity_id", "text")
        },
    )
    if not details_adopted:
        op.create_table(
            "narrative_details",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("campaign_id", sa.String(length=36), nullable=False),
            sa.Column("scene_id", sa.String(length=36), nullable=False),
            sa.Column("source_turn_id", sa.String(length=36), nullable=True),
            sa.Column("subject_entity_id", sa.String(length=36), nullable=True),
            sa.Column("detail_type", sa.String(length=50), nullable=False),
            sa.Column("text", sa.Text(), nullable=False),
            sa.Column("visibility", sa.String(length=50), nullable=False),
            sa.Column("turn_window", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint(
                "detail_type IN ('ambient', 'sensory', 'gaze', 'expression', "
                "'gesture', 'pose', 'spatial', 'other')",
                name="ck_narrative_detail_type",
            ),
            sa.CheckConstraint(
                "turn_window >= 1 AND turn_window <= 12",
                name="ck_narrative_detail_turn_window",
            ),
            sa.ForeignKeyConstraint(
                ["campaign_id"], ["campaigns.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["scene_id"], ["scenes.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["source_turn_id"], ["turns.id"], ondelete="SET NULL"
            ),
            sa.ForeignKeyConstraint(
                ["subject_entity_id"], ["entities.id"], ondelete="SET NULL"
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "scene_id",
                "source_turn_id",
                "subject_entity_id",
                "text",
                name="uq_narrative_detail_source",
            ),
        )
    ensure_index("narrative_details", "ix_narrative_details_campaign_id", ["campaign_id"])
    ensure_index("narrative_details", "ix_narrative_details_scene_id", ["scene_id"])
    ensure_index(
        "narrative_details",
        "ix_narrative_details_source_turn_id",
        ["source_turn_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_narrative_details_source_turn_id", table_name="narrative_details")
    op.drop_index("ix_narrative_details_scene_id", table_name="narrative_details")
    op.drop_index("ix_narrative_details_campaign_id", table_name="narrative_details")
    op.drop_table("narrative_details")
    op.drop_index(
        "ix_fact_memory_profiles_subject_entity_id",
        table_name="fact_memory_profiles",
    )
    op.drop_index(
        "ix_fact_memory_profiles_memory_kind",
        table_name="fact_memory_profiles",
    )
    op.drop_table("fact_memory_profiles")
