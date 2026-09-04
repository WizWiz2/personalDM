from __future__ import annotations

import sqlite3
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

import app.db.action_sequence_table  # noqa: F401
import app.db.campaign_setup_table  # noqa: F401
import app.db.generation_lifecycle_table  # noqa: F401
import app.db.memory_taxonomy_table  # noqa: F401
import app.db.narration_validation_table  # noqa: F401
import app.db.scene_bridge_table  # noqa: F401
import app.db.scene_location_table  # noqa: F401
import app.db.scene_state_table  # noqa: F401
import app.db.scene_transition_table  # noqa: F401
import app.db.tables  # noqa: F401
import app.db.thesis_lifecycle_table  # noqa: F401
import app.db.truth_engine_table  # noqa: F401
from app.config import settings
from app.db.engine import Base


BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _database_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.resolve().as_posix()}"


def _upgrade(path: Path, revision: str) -> None:
    url = _database_url(path)
    previous = settings.DATABASE_URL
    try:
        settings.DATABASE_URL = url
        config = Config(str(BACKEND_ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
        config.set_main_option(
            "version_locations",
            str(BACKEND_ROOT / "alembic" / "versions"),
        )
        config.set_main_option("sqlalchemy.url", url)
        command.upgrade(config, revision)
    finally:
        settings.DATABASE_URL = previous


def _create_current_orm_tables(path: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{path.resolve().as_posix()}")
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()


def test_upgrade_head_adopts_full_precreated_orm_chain_and_preserves_rows(tmp_path) -> None:
    database = tmp_path / "legacy-drift.db"

    # Reproduce the historical split-brain state: Alembic is stamped at the last
    # pre-scene-structure revision, but an older runtime has already created every
    # then-current ORM table directly.
    _upgrade(database, "e3c4d5e6f7a8")
    _create_current_orm_tables(database)

    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO campaigns (id, name, created_at, updated_at)
            VALUES ('identity-campaign', 'Identity migration', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """
        )
        connection.execute(
            """
            INSERT INTO entities (
                id,
                campaign_id,
                entity_type,
                canonical_name,
                aliases,
                description,
                status,
                provenance,
                version,
                custom_fields,
                created_at,
                updated_at
            ) VALUES (
                'legacy-guard',
                'identity-campaign',
                'character',
                'Guard',
                NULL,
                'Existing identity must survive the table recreation.',
                'active',
                'legacy-runtime',
                1,
                NULL,
                CURRENT_TIMESTAMP,
                CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            "INSERT INTO characters (entity_id) VALUES ('legacy-guard')"
        )
        connection.execute(
            """
            INSERT INTO scene_transitions (
                id,
                campaign_id,
                source_scene_id,
                target_scene_id,
                trigger_turn_id,
                transition_type,
                status,
                source_location_id,
                target_location_id,
                elapsed_time,
                time_after,
                reason,
                detector,
                created_at,
                undone_at
            ) VALUES (
                'legacy-transition',
                'legacy-campaign',
                NULL,
                'legacy-scene',
                NULL,
                'location_transition',
                'applied',
                NULL,
                NULL,
                NULL,
                NULL,
                'preserve me',
                'legacy-runtime',
                CURRENT_TIMESTAMP,
                NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO generation_lifecycles (
                generation_run_id,
                phase,
                attempt,
                updated_at
            ) VALUES ('legacy-generation', 'received', 1, CURRENT_TIMESTAMP)
            """
        )
        connection.commit()

    _upgrade(database, "head")

    with sqlite3.connect(database) as connection:
        revision = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()
        transition = connection.execute(
            "SELECT reason, detector FROM scene_transitions WHERE id = 'legacy-transition'"
        ).fetchone()
        lifecycle = connection.execute(
            "SELECT phase, attempt FROM generation_lifecycles "
            "WHERE generation_run_id = 'legacy-generation'"
        ).fetchone()
        preserved_entity = connection.execute(
            "SELECT canonical_name, description FROM entities WHERE id = 'legacy-guard'"
        ).fetchone()
        preserved_character = connection.execute(
            "SELECT entity_id FROM characters WHERE entity_id = 'legacy-guard'"
        ).fetchone()

        # TE2 identity is UUID-based. A second distinct entity is allowed to keep the same
        # human-facing role label; no synthetic suffix is needed to satisfy persistence.
        connection.execute(
            """
            INSERT INTO entities (
                id,
                campaign_id,
                entity_type,
                canonical_name,
                status,
                provenance,
                version,
                created_at,
                updated_at
            ) VALUES (
                'second-guard',
                'identity-campaign',
                'character',
                'Guard',
                'active',
                'semantic_compiler',
                1,
                CURRENT_TIMESTAMP,
                CURRENT_TIMESTAMP
            )
            """
        )
        connection.commit()
        duplicate_labels = connection.execute(
            """
            SELECT id FROM entities
            WHERE campaign_id = 'identity-campaign'
              AND entity_type = 'character'
              AND canonical_name = 'Guard'
            ORDER BY id
            """
        ).fetchall()

        indexes = {
            row[1]
            for row in connection.execute(
                "PRAGMA index_list('scene_transitions')"
            ).fetchall()
        }
        semantic_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info('semantic_types')"
            ).fetchall()
        }
        truth_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

    assert revision == ("a7b8c9d0e1f2",)
    assert transition == ("preserve me", "legacy-runtime")
    assert lifecycle == ("received", 1)
    assert preserved_entity == (
        "Guard",
        "Existing identity must survive the table recreation.",
    )
    assert preserved_character == ("legacy-guard",)
    assert duplicate_labels == [("legacy-guard",), ("second-guard",)]
    assert "ix_scene_transitions_campaign_id" in indexes
    assert "system_key" in semantic_columns
    assert {
        "truth_event_records",
        "truth_event_effects",
        "semantic_types",
        "fluent_assertions",
        "world_relation_assertions",
        "entity_mentions",
    }.issubset(truth_tables)
