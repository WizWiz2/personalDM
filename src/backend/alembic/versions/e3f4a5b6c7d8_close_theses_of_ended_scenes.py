"""Theses are scene working memory: close those still active in ended scenes."""
from alembic import op
import sqlalchemy as sa

revision = "e3f4a5b6c7d8"
down_revision = "d2e3f4a5b6c7"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    stale = [row[0] for row in bind.execute(sa.text(
        "SELECT t.id FROM scene_theses t JOIN scenes s ON s.id = t.scene_id "
        "WHERE t.status = 'active' AND s.status != 'active'"
    ))]
    for thesis_id in stale:
        bind.execute(sa.text("UPDATE scene_theses SET status = 'resolved' WHERE id = :id"), {"id": thesis_id})
        bind.execute(
            sa.text("UPDATE thesis_lifecycle_profiles SET closure_reason = 'scene_closed' WHERE thesis_id = :id"),
            {"id": thesis_id},
        )


def downgrade():
    pass
