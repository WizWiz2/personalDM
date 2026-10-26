"""N-7: safely upgrade a legacy PersonalDM SQLite database before serving requests.

Unversioned databases from former create_all bootstraps are classified by
schema fingerprints, never silently stamped as 'head'. Copy-on-start provides
a recoverable backup before the first destructive schema operation.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.engine import make_url

from app.config import settings
from app.runtime_paths import backend_dir

LEGACY_PRE_PLACEMENT_REVISION = "f3a4b5c6d7e8"


def classify_unversioned_schema(
    tables: set[str], items: set[str], steps: set[str],
    campaigns: set[str] | None = None,
) -> str:
    """Return the only safe Alembic stamp target for a known create_all schema."""
    domain_tables = tables - {"sqlite_sequence", "alembic_version"}
    if not domain_tables:
        return "empty"
    # A corrupt campaigns table used to pass legacy fingerprints and be
    # stamped as a valid schema; subsequent API reads then failed with 500.
    if campaigns is not None and not {"id", "name"} <= campaigns:
        return "unknown"
    final = (
        {"campaigns", "items", "action_steps", "npc_identity_anchors",
         "item_custody_commands", "memory_witnesses"} <= tables
        and {"placement_detail"} <= items
        and {"item_previous_placement_detail", "item_result_placement_detail"} <= steps
    )
    if final:
        return "head"
    prior = (
        {"campaigns", "items", "action_steps", "memory_witnesses",
         "action_sequences"} <= tables
        and {"item_id", "item_previous_owner_id"} <= steps
        and "item_previous_placement_detail" not in steps
        and "placement_detail" not in items
        and "npc_identity_anchors" not in tables
        and "item_custody_commands" not in tables
    )
    if prior:
        return LEGACY_PRE_PLACEMENT_REVISION
    return "unknown"


def _migrate_sqlite() -> None:
    database_url = make_url(settings.DATABASE_URL)
    if not database_url.drivername.startswith("sqlite"):
        return  # SQLite legacy format is the only implicit migration contract.
    filename = database_url.database
    if not filename or filename == ":memory:":
        return
    db = Path(filename).resolve()
    db.parent.mkdir(parents=True, exist_ok=True)
    ini = backend_dir() / "alembic.ini"
    script_dir = backend_dir() / "alembic"
    if not ini.is_file() or not script_dir.is_dir():
        raise RuntimeError("Alembic migration resources are missing from PersonalDM installation")

    cfg = Config(str(ini))
    cfg.set_main_option("script_location", str(script_dir))
    cfg.set_main_option("sqlalchemy.url", settings.DATABASE_URL.replace("%", "%%"))
    head = ScriptDirectory.from_config(cfg).get_current_head()

    original_exists = db.exists()
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        tables = {name for name, in rows}
        columns = lambda name: {r[1] for r in conn.execute(f"PRAGMA table_info({name})")}
        # A current DB must not generate another full backup on every start.
        if "alembic_version" in tables:
            revisions = {
                row[0] for row in conn.execute("SELECT version_num FROM alembic_version")
            }
            if revisions == {head}:
                if "campaigns" not in tables or not {"id", "name"} <= columns("campaigns"):
                    raise RuntimeError("PersonalDM schema mismatch: campaigns.id/name missing; database not modified")
                return
        decision = (
            "versioned"
            if "alembic_version" in tables
            else classify_unversioned_schema(
                tables,
                columns("items") if "items" in tables else set(),
                columns("action_steps") if "action_steps" in tables else set(),
                columns("campaigns") if "campaigns" in tables else set(),
            )
        )
        if decision == "unknown":
            raise RuntimeError(
                "Unversioned PersonalDM database has an unknown schema. "
                "Automatic stamp is unsafe; back up the database and migrate explicitly."
            )
        if original_exists and (tables - {"sqlite_sequence"}):
            destination = db.with_name(
                f"{db.name}.pre-migration-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.bak"
            )
            # SQLite online backup includes committed WAL pages, unlike shutil.copyfile.
            with sqlite3.connect(destination) as backup:
                conn.backup(backup)

    if decision not in {"versioned", "empty"}:
        command.stamp(cfg, decision)
    command.upgrade(cfg, "head")


async def migrate_database_on_startup() -> None:
    """Run Alembic off the application event loop before the worker starts."""
    await asyncio.to_thread(_migrate_sqlite)


__all__ = ["migrate_database_on_startup", "classify_unversioned_schema"]


if __name__ == "__main__":
    _migrate_sqlite()
