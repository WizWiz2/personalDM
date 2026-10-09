"""Reserve SQLite's writer before reading guards for an atomic write batch."""
import asyncio
import sqlite3

from sqlalchemy import text
from sqlalchemy.exc import OperationalError


async def reserve_sqlite_writer(session) -> None:
    """Start a fresh write transaction; never upgrade a stale WAL read snapshot.

    Call after model work and rollback, before reading the source-turn guards.
    Only lock acquisition retries; extraction and canon writes are not replayed.
    Other databases keep their normal transaction isolation.
    """
    if session.bind.dialect.name != "sqlite":
        return
    for attempt in range(3):
        try:
            await session.execute(text("BEGIN IMMEDIATE"))
            return
        except OperationalError as exc:
            await session.rollback()
            code = getattr(exc.orig, "sqlite_errorcode", None)
            if code is None or code & 0xFF not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise
            if attempt == 2:
                raise
            await asyncio.sleep(0.05 * (2 ** attempt))
