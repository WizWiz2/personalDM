"""Bind legacy subjects exactly; retire ambiguous and duplicate physical projections."""
import json

import sqlalchemy as sa

from alembic import op

revision = "f4a5b6c7d8e9"
down_revision = "e3f4a5b6c7d8"
branch_labels = None
depends_on = None


def _key(value):
    return " ".join(str(value or "").casefold().split())


def upgrade():
    bind = op.get_bind()
    identities = {}
    for row in bind.execute(sa.text("SELECT id, campaign_id, canonical_name, aliases FROM entities")):
        for name in [row.canonical_name, *json.loads(row.aliases or "[]")]:
            identities.setdefault((row.campaign_id, _key(name)), set()).add(row.id)
    facts = bind.execute(sa.text(
        "SELECT f.id, f.campaign_id, f.subject, f.predicate, p.subject_entity_id, p.fact_id "
        "FROM facts f LEFT JOIN fact_memory_profiles p ON p.fact_id=f.id WHERE f.is_current=1"
    )).fetchall()
    for fact in facts:
        candidates = identities.get((fact.campaign_id, _key(fact.subject)), set())
        subject = fact.subject_entity_id or (next(iter(candidates)) if len(candidates) == 1 else None)
        if not subject or fact.predicate in {"holder", "location"}:
            # Keep the source row and its provenance for historical inspection.
            bind.execute(sa.text("UPDATE facts SET is_current=0 WHERE id=:id"), {"id": fact.id})
        elif fact.fact_id:
            bind.execute(sa.text("UPDATE fact_memory_profiles SET subject_entity_id=:subject WHERE fact_id=:id"),
                         {"subject": subject, "id": fact.id})
        else:
            bind.execute(sa.text(
                "INSERT INTO fact_memory_profiles (fact_id,memory_kind,subject_entity_id,created_at,updated_at) "
                "SELECT id,CASE WHEN scope='scene' THEN 'scene_state' ELSE 'world_canon' END,:subject,"
                "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP FROM facts WHERE id=:id"
            ), {"subject": subject, "id": fact.id})

    # Several spelling/alias keys may now denote one entity. Reconcile single
    # slots once more by identity, preserving the newest source and its history.
    singles = {"condition", "activity", "access_state", "supply_level", "quantity",
               "operational_state", "role", "allegiance", "agreement"}
    newest = {}
    for fact in bind.execute(sa.text(
        "SELECT f.id,f.campaign_id,f.predicate,p.subject_entity_id FROM facts f "
        "JOIN fact_memory_profiles p ON p.fact_id=f.id WHERE f.is_current=1 "
        "ORDER BY f.updated_at DESC,f.created_at DESC,f.id DESC"
    )).fetchall():
        if fact.predicate not in singles:
            continue
        key = (fact.campaign_id, fact.subject_entity_id, fact.predicate)
        if key in newest:
            bind.execute(sa.text("UPDATE facts SET is_current=0,superseded_by=:new WHERE id=:id"),
                         {"new": newest[key], "id": fact.id})
        else:
            newest[key] = fact.id


def downgrade():
    # Binding is an identity correction; retired history must not be made current blindly.
    pass
