from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config

from app.config import settings


def database_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.resolve().as_posix()}"


def _upgrade_simulation_database_sync(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    backend_root = Path(__file__).resolve().parents[1]
    alembic_ini = backend_root / "alembic.ini"
    if not alembic_ini.exists():
        raise RuntimeError(f"Alembic config not found: {alembic_ini}")

    url = database_url(path)
    previous = settings.DATABASE_URL
    try:
        settings.DATABASE_URL = url
        config = Config(str(alembic_ini))
        config.set_main_option("script_location", str(backend_root / "alembic"))
        config.set_main_option("sqlalchemy.url", url)
        command.upgrade(config, "head")
    finally:
        settings.DATABASE_URL = previous

    with sqlite3.connect(path) as connection:
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        if not row or not row[0]:
            raise RuntimeError("Alembic upgrade completed without alembic_version")
        return str(row[0])


async def upgrade_simulation_database(path: Path) -> str:
    """Run the real Alembic chain without nesting event loops."""
    return await asyncio.to_thread(_upgrade_simulation_database_sync, path)


def current_revision(path: Path) -> str | None:
    if not path.exists():
        return None
    with sqlite3.connect(path) as connection:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='alembic_version'"
        ).fetchone()
        if not exists:
            return None
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        return str(row[0]) if row and row[0] else None
