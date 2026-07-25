"""Add scene scope to durable facts.

Revision ID: e3c4d5e6f7a8
Revises: d2b3c4d5e6f7
Create Date: 2026-07-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e3c4d5e6f7a8"
down_revision: str | None = "d2b3c4d5e6f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("facts") as batch:
        batch.add_column(
            sa.Column(
                "scope",
                sa.String(length=50),
                nullable=False,
                server_default="campaign",
            )
        )
        batch.add_column(sa.Column("scene_id", sa.String(length=36), nullable=True))
        batch.create_foreign_key(
            "fk_facts_scene_id_scenes",
            "scenes",
            ["scene_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch.create_check_constraint(
            "ck_fact_scope_scene",
            "(scope = 'campaign' AND scene_id IS NULL) OR "
            "(scope = 'scene' AND scene_id IS NOT NULL)",
        )
        batch.create_index(
            "ix_facts_scope_scene",
            ["campaign_id", "scope", "scene_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("facts") as batch:
        batch.drop_index("ix_facts_scope_scene")
        batch.drop_constraint("ck_fact_scope_scene", type_="check")
        batch.drop_constraint("fk_facts_scene_id_scenes", type_="foreignkey")
        batch.drop_column("scene_id")
        batch.drop_column("scope")
