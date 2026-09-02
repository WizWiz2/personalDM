from __future__ import annotations

import sqlite3
from pathlib import Path

from live_model_contracts.oracle_snapshot import ORACLE_SCHEMA, _fact_rows


def test_fact_snapshot_reads_memory_profile_from_real_storage_shape(tmp_path: Path) -> None:
    db = sqlite3.connect(tmp_path / "oracle.db")
    try:
        db.executescript(
            """
            CREATE TABLE facts (
                id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL,
                subject TEXT NOT NULL,
                predicate TEXT NOT NULL,
                object_value TEXT,
                truth_status TEXT NOT NULL,
                scope TEXT NOT NULL,
                is_current INTEGER NOT NULL,
                created_at TEXT
            );
            CREATE TABLE fact_memory_profiles (
                fact_id TEXT PRIMARY KEY,
                memory_kind TEXT NOT NULL,
                subject_entity_id TEXT
            );
            INSERT INTO facts (
                id, campaign_id, subject, predicate, object_value,
                truth_status, scope, is_current, created_at
            ) VALUES (
                'fact-1', 'campaign-1', 'door', 'state', 'open',
                'true', 'campaign', 1, '2026-09-03T00:00:00'
            );
            INSERT INTO fact_memory_profiles (
                fact_id, memory_kind, subject_entity_id
            ) VALUES ('fact-1', 'entity_state', 'entity-1');
            """
        )
        rows = _fact_rows(db, "campaign-1")
    finally:
        db.close()

    assert len(rows) == 1
    assert rows[0]["memory_kind"] == "entity_state"
    assert rows[0]["subject_entity_id"] == "entity-1"


def test_fact_snapshot_falls_back_for_legacy_row_without_profile(tmp_path: Path) -> None:
    db = sqlite3.connect(tmp_path / "oracle-legacy.db")
    try:
        db.executescript(
            """
            CREATE TABLE facts (
                id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL,
                subject TEXT NOT NULL,
                predicate TEXT NOT NULL,
                object_value TEXT,
                truth_status TEXT NOT NULL,
                scope TEXT NOT NULL,
                is_current INTEGER NOT NULL,
                created_at TEXT
            );
            CREATE TABLE fact_memory_profiles (
                fact_id TEXT PRIMARY KEY,
                memory_kind TEXT NOT NULL,
                subject_entity_id TEXT
            );
            INSERT INTO facts (
                id, campaign_id, subject, predicate, object_value,
                truth_status, scope, is_current, created_at
            ) VALUES (
                'fact-1', 'campaign-1', 'scene', 'weather', 'rain',
                'true', 'scene', 1, '2026-09-03T00:00:00'
            );
            """
        )
        rows = _fact_rows(db, "campaign-1")
    finally:
        db.close()

    assert rows[0]["memory_kind"] == "scene_state"
    assert rows[0]["subject_entity_id"] is None


def test_oracle_schema_contract_matches_current_orm_tables() -> None:
    # Import every table module used by the oracle so SQLAlchemy metadata is complete.
    import app.db.action_sequence_table  # noqa: F401
    import app.db.memory_taxonomy_table  # noqa: F401
    import app.db.scene_location_table  # noqa: F401
    import app.db.scene_state_table  # noqa: F401
    import app.db.tables  # noqa: F401
    from app.db.engine import Base

    problems: list[str] = []
    for table_name, required_columns in ORACLE_SCHEMA.items():
        table = Base.metadata.tables.get(table_name)
        if table is None:
            problems.append(f"missing table {table_name}")
            continue
        actual_columns = set(table.columns.keys())
        missing = sorted(required_columns - actual_columns)
        if missing:
            problems.append(f"{table_name}: missing {', '.join(missing)}")

    assert not problems, "; ".join(problems)
