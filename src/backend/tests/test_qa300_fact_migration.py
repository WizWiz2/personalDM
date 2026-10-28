"""Upgrade legacy canon without inventing group identity or losing source history."""
import sqlite3

from alembic.config import Config

from alembic import command
from app.config import settings
from app.runtime_paths import backend_dir


def test_subject_migration_binds_exact_aliases_and_retires_ambiguous_history(tmp_path, monkeypatch):
    path = tmp_path / "canon.db"
    monkeypatch.setattr(settings, "DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    cfg = Config()
    cfg.set_main_option("script_location", str(backend_dir() / "alembic"))
    command.upgrade(cfg, "e3f4a5b6c7d8")
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO campaigns (id,name,created_at,updated_at) VALUES ('c','x',datetime(),datetime())")
        conn.execute("INSERT INTO entities (id,campaign_id,entity_type,canonical_name,aliases,description,status,provenance,version,created_at,updated_at) "
                     "VALUES ('person','c','character','Хранитель','[\"Господин хранитель\"]','','active','manual',1,datetime(),datetime())")
        for i, (subject, predicate, value) in enumerate([
            ("Хранитель", "condition", "спокоен"),
            ("Господин хранитель", "condition", "встревожен"),
            ("Хранитель", "location", "маяк"),
            ("Два бланка", "holder", "Хранитель"),
            ("Два бланка", "known_feature", "не выданы"),
        ]):
            conn.execute("INSERT INTO facts (id,campaign_id,subject,predicate,object_value,truth_status,confidence,visibility,scope,is_current,created_at,updated_at) "
                         "VALUES (?, 'c', ?, ?, ?, 'true',1,'dm','campaign',1,datetime('now',?),datetime('now',?))",
                         (f"f{i}",subject,predicate,value,f"+{i} minutes",f"+{i} minutes"))
    command.upgrade(cfg, "head")
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM facts").fetchone()[0] == 5
        current = conn.execute("SELECT f.id,f.object_value,p.subject_entity_id FROM facts f "
                               "JOIN fact_memory_profiles p ON p.fact_id=f.id WHERE f.is_current=1").fetchall()
        assert current == [("f1", "встревожен", "person")]
        assert conn.execute("SELECT superseded_by FROM facts WHERE id='f0'").fetchone()[0] == "f1"
