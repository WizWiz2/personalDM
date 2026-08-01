"""Add authoritative structured locations to scenes.

Revision ID: f4d5e6a7b8c9
Revises: e3c4d5e6f7a8
Create Date: 2026-08-01
"""

from alembic import op
import sqlalchemy as sa


revision = "f4d5e6a7b8c9"
down_revision = "e3c4d5e6f7a8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scene_location_links",
        sa.Column("scene_id", sa.String(length=36), nullable=False),
        sa.Column("location_id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["scene_id"],
            ["scenes.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["location_id"],
            ["entities.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("scene_id"),
    )
    op.create_index(
        "ix_scene_location_links_location_id",
        "scene_location_links",
        ["location_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_scene_location_links_location_id",
        table_name="scene_location_links",
    )
    op.drop_table("scene_location_links")
