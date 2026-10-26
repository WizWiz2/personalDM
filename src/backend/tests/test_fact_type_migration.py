"""Legacy free-text predicates: the newest current value per (subject, predicate) survives."""
import sqlite3

from alembic import command
from alembic.config import Config

from app.config import settings
from app.runtime_paths import backend_dir


def test_migration_collapses_parallel_current_values(tmp_path, monkeypatch):
    db = tmp_path / "legacy.db"
    monkeypatch.setattr(settings, "DATABASE_URL", f"sqlite+aiosqlite:///{db}")
    cfg = Config(str(backend_dir() / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend_dir() / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db}")
    command.upgrade(cfg, "c1f2a3b4c5d6")
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO campaigns (id, name, created_at, updated_at) VALUES ('c','x',datetime(),datetime())")
        for index, value in enumerate(("аппаратная", "маяк", "причал")):
            conn.execute(
                "INSERT INTO facts (id, campaign_id, subject, predicate, object_value, truth_status, "
                "confidence, visibility, scope, is_current, created_at, updated_at) VALUES "
                f"('f{index}','c','Йонас','местонахождение',?, 'true', 1, 'dm', 'campaign', 1, "
                f"datetime('now','+{index} minutes'), datetime())",
                (value,),
            )
    command.upgrade(cfg, "d2e3f4a5b6c7")
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT object_value FROM facts WHERE is_current = 1").fetchall()
    assert rows == [("причал",)]
