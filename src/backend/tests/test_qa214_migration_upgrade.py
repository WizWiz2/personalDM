"""N-7 integration: an unversioned QA-213 pre-placement DB upgrades without manual stamp."""
import asyncio
import sqlite3

from alembic import command
from alembic.config import Config
import pytest

from app.config import settings
from app.db.startup_migrations import _migrate_sqlite
from app.runtime_paths import backend_dir


@pytest.mark.asyncio
async def test_unversioned_preplacement_db_gets_backup_and_new_columns(tmp_path, monkeypatch):
    db = tmp_path / "qa213-unversioned.db"
    url = f"sqlite+aiosqlite:///{db}"
    monkeypatch.setattr(settings, "DATABASE_URL", url)

    def prepare_previous_leaf():
        cfg = Config(str(backend_dir() / "alembic.ini"))
        cfg.set_main_option("script_location", str(backend_dir() / "alembic"))
        cfg.set_main_option("sqlalchemy.url", url)
        command.upgrade(cfg, "f3a4b5c6d7e8")
        with sqlite3.connect(db) as conn:
            conn.execute("DROP TABLE alembic_version")
            assert "item_previous_placement_detail" not in {
                row[1] for row in conn.execute("PRAGMA table_info(action_steps)")
            }

    await asyncio.to_thread(prepare_previous_leaf)
    await asyncio.to_thread(_migrate_sqlite)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "c2d3e4f5a6b7"
        assert {"source_step_id", "item_id", "content"} <= {
            row[1] for row in conn.execute("PRAGMA table_info(item_content_disclosures)")
        }
        assert {"source_step_id", "item_id", "property_name", "value"} <= {
            row[1] for row in conn.execute("PRAGMA table_info(item_property_effects)")
        }
        assert "source_action_index" in {
            row[1] for row in conn.execute("PRAGMA table_info(action_steps)")
        }
        assert {"item_previous_placement_detail", "item_result_placement_detail"} <= {
            row[1] for row in conn.execute("PRAGMA table_info(action_steps)")
        }
        assert {"npc_identity_anchors", "item_custody_commands"} <= {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    backups = list(tmp_path.glob("qa213-unversioned.db.pre-migration-*.bak"))
    assert len(backups) == 1

    # A second start at head neither changes the schema nor creates a backup.
    await asyncio.to_thread(_migrate_sqlite)
    assert len(list(tmp_path.glob("qa213-unversioned.db.pre-migration-*.bak"))) == 1
