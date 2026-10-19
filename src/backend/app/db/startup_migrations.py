"""N-7: safely migrate known SQLite schemas before serving user campaigns."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine.url import make_url

from app.config import settings
from app.db.engine import Base
from app.runtime_paths import backend_dir

# Register the complete production declarative schema for strict full-schema adoption.
from app.db import (  # noqa: F401
    action_sequence_table, campaign_setup_table, generation_lifecycle_table,
    memory_taxonomy_table, narration_validation_table, scene_bridge_table,
    scene_location_table, scene_state_table, scene_transition_table,
    thesis_lifecycle_table, truth_engine_table, tables,
)

logger = logging.getLogger(__name__)
LEGACY_F3 = "f3a4b5c6d7e8"


def classify_unversioned_schema(columns: dict[str, set[str]]) -> str | None:
    """Only adopt a schema we can recognize; never stamp an arbitrary user DB."""
    if not columns:
        return "base"
    if all(
        table in columns and set(model.columns.keys()).issubset(columns[table])
        for table, model in Base.metadata.tables.items()
    ):
        return "head"
    # QA-213 legacy pre-placement create_all schema. Exact sentinels ensure
    # newer-looking but incomplete schemas cannot masquerade as this revision.
    if (
        {"campaigns", "turns", "items", "action_steps", "memory_witnesses",
         "scene_transitions", "generation_runs"}.issubset(columns)
        and {"item_id", "item_operation", "item_previous_owner_id",
             "item_result_owner_id"}.issubset(columns["action_steps"])
        and "item_previous_placement_detail" not in columns["action_steps"]
        and "placement_detail" not in columns["items"]
        and "npc_identity_anchors" not in columns
        and "item_custody_commands" not in columns
    ):
        return LEGACY_F3
    return None


def _sqlite_backup(database_path: Path) -> Path:
    """SQLite online backup is consistent even when the DB uses WAL."""
    backup_path = database_path.with_name(
        database_path.name + ".pre-qa214-" +
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".bak"
    )
    with sqlite3.connect(str(database_path)) as source:
        with sqlite3.connect(str(backup_path)) as dest:
            source.backup(dest)
    return backup_path


def _migrate_sqlite() -> None:
    db_url = make_url(settings.DATABASE_URL)
    if db_url.get_backend_name() != "sqlite" or db_url.database in (None, ":memory:"):
        return
    path = Path(db_url.database).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(path)) as connection:
        cursor = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
        names = {row[0] for row in cursor.fetchall()}
        columns = {
            name: {row[1] for row in connection.execute(
                'PRAGMA table_info("' + name.replace('"', '""') + '")'
            )}
            for name in names if name != "alembic_version"
        }
        versioned = "alembic_version" in names
        if versioned:
            versions = [r[0] for r in connection.execute("SELECT version_num FROM alembic_version")]
            if not versions:
                raise RuntimeError("N-7: Alembic version table is empty; manual schema review required")
            baseline = None
        else:
            baseline = classify_unversioned_schema(columns)
            if baseline is None:
                raise RuntimeError(
                    "N-7: unversioned database schema is unknown. No automatic stamp was "
                    "performed; back up this database and review its Alembic history."
                )
    cfg_path = backend_dir() / "alembic.ini"
    if not cfg_path.is_file():
        raise RuntimeError(f"N-7: migration config is missing: {cfg_path}")
    cfg = Config(str(cfg_path))
    cfg.set_main_option("script_location", str(backend_dir() / "alembic"))
    cfg.set_main_option("sqlalchemy.url", settings.DATABASE_URL.replace("%", "%%"))

    # Database must be backed up before either stamp or upgrade mutates it.
    # Empty new files contain no user data; other paths always get a backup.
    if names:
        backup = _sqlite_backup(path)
        logger.warning("N-7: pre-migration database backup: %s", backup)
    if baseline and baseline != "base":
        command.stamp(cfg, baseline)
        logger.warning("N-7: adopted independently verified SQLite schema at %s", baseline)
    command.upgrade(cfg, "head")


async def upgrade_schema_before_startup() -> None:
    # Alembic's env.py uses asyncio.run. Execute it in a worker thread rather
    # than inside FastAPI's running event loop.
    await asyncio.to_thread(_migrate_sqlite)
