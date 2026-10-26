"""Typed fact predicates: collapse contradictory legacy current values.

New facts carry a catalog ``fact_type`` ID as predicate. Legacy free-text predicates stay
readable as ``legacy_note`` (multi). Before this revision a free-text predicate written in
another scene opened a parallel current value, so the same (subject, predicate text) could hold
several current values. Keep the newest one current and supersede the rest.
"""
from alembic import op
import sqlalchemy as sa

revision = "d2e3f4a5b6c7"
down_revision = "c1f2a3b4c5d6"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    rows = bind.execute(sa.text(
        "SELECT f.id, f.campaign_id, f.subject, f.predicate, p.subject_entity_id "
        "FROM facts f LEFT JOIN fact_memory_profiles p ON p.fact_id = f.id "
        "WHERE f.is_current = 1 ORDER BY f.created_at DESC, f.id DESC"
    )).fetchall()
    newest: dict[tuple, str] = {}
    for fact_id, campaign_id, subject, predicate, entity_id in rows:
        key = (
            campaign_id,
            entity_id or " ".join(str(subject or "").casefold().split()),
            " ".join(str(predicate or "").casefold().split()),
        )
        keeper = newest.setdefault(key, fact_id)
        if keeper != fact_id:
            bind.execute(
                sa.text("UPDATE facts SET is_current = 0, superseded_by = :keeper WHERE id = :id"),
                {"keeper": keeper, "id": fact_id},
            )


def downgrade():
    pass
