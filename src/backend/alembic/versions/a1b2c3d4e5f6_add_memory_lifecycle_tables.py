"""Add memory classification and transient narrative details.

Revision ID: a1b2c3d4e5f6
Revises: f0a1b2c3d4e5
Create Date: 2026-08-02
"""

from alembic import op
import sqlalchemy as sa


revision = "a1b2c3d4e5f6"
down_revision = "f0a1b2c3d4e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fact_memory_links",
        sa.Column("fact_id", sa.String(length=36), nullable=False),
        sa.Column("memory_class", sa.String(length=32), nullable=False),
        sa.Column("subject_entity_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["fact_id"],
            ["facts.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["subject_entity_id"],
            ["entities.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("fact_id"),
    )
    op.create_index(
        op.f("ix_fact_memory_links_subject_entity_id"),
        "fact_memory_links",
        ["subject_entity_id"],
        unique=False,
    )

    op.create_table(
        "narrative_details",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("campaign_id", sa.String(length=36), nullable=False),
        sa.Column("scene_id", sa.String(length=36), nullable=False),
        sa.Column("source_turn_id", sa.String(length=36), nullable=False),
        sa.Column("detail_type", sa.String(length=64), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("participant_ids", sa.Text(), nullable=True),
        sa.Column("salience", sa.Float(), nullable=False),
        sa.Column("ttl_turns", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["campaigns.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["scene_id"],
            ["scenes.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_turn_id"],
            ["turns.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_turn_id",
            "text",
            name="uq_narrative_detail_source_text",
        ),
    )
    op.create_index(
        op.f("ix_narrative_details_campaign_id"),
        "narrative_details",
        ["campaign_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_narrative_details_scene_id"),
        "narrative_details",
        ["scene_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_narrative_details_source_turn_id"),
        "narrative_details",
        ["source_turn_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_narrative_details_source_turn_id"),
        table_name="narrative_details",
    )
    op.drop_index(
        op.f("ix_narrative_details_scene_id"),
        table_name="narrative_details",
    )
    op.drop_index(
        op.f("ix_narrative_details_campaign_id"),
        table_name="narrative_details",
    )
    op.drop_table("narrative_details")
    op.drop_index(
        op.f("ix_fact_memory_links_subject_entity_id"),
        table_name="fact_memory_links",
    )
    op.drop_table("fact_memory_links")
