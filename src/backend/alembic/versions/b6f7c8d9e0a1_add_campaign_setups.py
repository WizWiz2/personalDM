"""Add persistent session-zero campaign contracts.

Revision ID: b6f7c8d9e0a1
Revises: a5e6f7b8c9d0
Create Date: 2026-08-02
"""

from alembic import op
import sqlalchemy as sa

from app.db.migration_compat import adopt_existing_table


revision = "b6f7c8d9e0a1"
down_revision = "a5e6f7b8c9d0"
branch_labels = None
depends_on = None

_TABLE = "campaign_setups"


def upgrade() -> None:
    adopted = adopt_existing_table(
        _TABLE,
        required_columns={
            "campaign_id",
            "status",
            "setting_name",
            "genre",
            "premise",
            "tone",
            "themes",
            "boundaries",
            "boundaries_confirmed",
            "rules_system",
            "world_summary",
            "starting_situation",
            "starting_location_id",
            "starting_scene_title",
            "play_style",
            "content_rating",
            "custom_fields",
            "completed_at",
            "created_at",
            "updated_at",
        },
        primary_key={"campaign_id"},
        non_nullable={
            "campaign_id",
            "status",
            "boundaries_confirmed",
            "created_at",
            "updated_at",
        },
        foreign_keys={
            (("campaign_id",), "campaigns", ("id",)),
            (("starting_location_id",), "entities", ("id",)),
        },
    )

    if not adopted:
        op.create_table(
            _TABLE,
            sa.Column("campaign_id", sa.String(length=36), nullable=False),
            sa.Column("status", sa.String(length=50), nullable=False),
            sa.Column("setting_name", sa.String(length=255), nullable=True),
            sa.Column("genre", sa.String(length=255), nullable=True),
            sa.Column("premise", sa.Text(), nullable=True),
            sa.Column("tone", sa.String(length=255), nullable=True),
            sa.Column("themes", sa.Text(), nullable=True),
            sa.Column("boundaries", sa.Text(), nullable=True),
            sa.Column("boundaries_confirmed", sa.Boolean(), nullable=False),
            sa.Column("rules_system", sa.String(length=255), nullable=True),
            sa.Column("world_summary", sa.Text(), nullable=True),
            sa.Column("starting_situation", sa.Text(), nullable=True),
            sa.Column("starting_location_id", sa.String(length=36), nullable=True),
            sa.Column("starting_scene_title", sa.String(length=255), nullable=True),
            sa.Column("play_style", sa.Text(), nullable=True),
            sa.Column("content_rating", sa.String(length=100), nullable=True),
            sa.Column("custom_fields", sa.Text(), nullable=True),
            sa.Column("completed_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(
                ["campaign_id"], ["campaigns.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["starting_location_id"], ["entities.id"], ondelete="SET NULL"
            ),
            sa.PrimaryKeyConstraint("campaign_id"),
        )

    # Backfill only campaigns that do not already have a setup row. This preserves
    # session-zero data from pre-migration ORM-created tables.
    op.execute(
        sa.text(
            """
            INSERT INTO campaign_setups (
                campaign_id,
                status,
                setting_name,
                genre,
                premise,
                tone,
                themes,
                boundaries,
                boundaries_confirmed,
                rules_system,
                world_summary,
                starting_situation,
                starting_location_id,
                starting_scene_title,
                play_style,
                content_rating,
                custom_fields,
                completed_at,
                created_at,
                updated_at
            )
            SELECT
                campaigns.id,
                'completed',
                campaigns.name,
                'legacy campaign',
                COALESCE(campaigns.description, campaigns.name),
                COALESCE(campaigns.narrative_style, 'existing campaign tone'),
                '[]',
                '[]',
                1,
                NULL,
                COALESCE(campaigns.description, campaigns.name),
                'Existing campaign state imported before session-zero enforcement',
                (
                    SELECT scene_location_links.location_id
                    FROM scene_location_links
                    WHERE scene_location_links.scene_id = campaigns.current_scene_id
                    LIMIT 1
                ),
                NULL,
                campaigns.narrative_style,
                NULL,
                '{"legacy_imported": true}',
                CURRENT_TIMESTAMP,
                CURRENT_TIMESTAMP,
                CURRENT_TIMESTAMP
            FROM campaigns
            WHERE NOT EXISTS (
                SELECT 1
                FROM campaign_setups existing
                WHERE existing.campaign_id = campaigns.id
            )
            """
        )
    )


def downgrade() -> None:
    op.drop_table(_TABLE)
