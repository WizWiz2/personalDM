"""Typed subject entity of a belief: the (holder, subject) supersession key."""

import sqlalchemy as sa
from alembic import op

revision = "a4b5c6d7e8f9"
down_revision = "f3a4b5c6d7e8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "subject_id" not in {c["name"] for c in sa.inspect(op.get_bind()).get_columns("beliefs")}:
        op.add_column("beliefs", sa.Column("subject_id", sa.String(length=36), nullable=True))


def downgrade() -> None:
    op.drop_column("beliefs", "subject_id")
