"""One scene per place: a transition may land in an existing scene."""

import sqlalchemy as sa
from alembic import op

revision = "e2f3a4b5c6d7"
down_revision = "d1e2f3a4b5c6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("scene_transitions")}
    if "target_reused" not in existing:
        op.add_column("scene_transitions", sa.Column(
            "target_reused", sa.Boolean(), nullable=False, server_default=sa.false()))
    if "previous_time_label" not in existing:
        op.add_column("scene_transitions", sa.Column(
            "previous_time_label", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("scene_transitions", "previous_time_label")
    op.drop_column("scene_transitions", "target_reused")
