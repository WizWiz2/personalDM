from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "f4d5e6a7b8c9_add_scene_location_links.py"
)


def _load_migration(connection):
    spec = importlib.util.spec_from_file_location("migration_f4d5e6a7b8c9_test", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations(MigrationContext.configure(connection))
    return module


def _create_parent_tables(connection) -> None:
    metadata = sa.MetaData()
    sa.Table(
        "entities",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True, nullable=False),
    )
    sa.Table(
        "scenes",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True, nullable=False),
    )
    metadata.create_all(connection)


def _create_legacy_link_table(connection) -> None:
    connection.exec_driver_sql(
        """
        CREATE TABLE scene_location_links (
            scene_id VARCHAR(36) NOT NULL,
            location_id VARCHAR(36) NOT NULL,
            PRIMARY KEY (scene_id),
            FOREIGN KEY(scene_id) REFERENCES scenes (id) ON DELETE CASCADE,
            FOREIGN KEY(location_id) REFERENCES entities (id) ON DELETE CASCADE
        )
        """
    )


def test_upgrade_adopts_compatible_existing_table_without_losing_rows() -> None:
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        _create_parent_tables(connection)
        _create_legacy_link_table(connection)
        connection.exec_driver_sql("INSERT INTO scenes (id) VALUES ('scene-1')")
        connection.exec_driver_sql("INSERT INTO entities (id) VALUES ('location-1')")
        connection.exec_driver_sql(
            "INSERT INTO scene_location_links (scene_id, location_id) "
            "VALUES ('scene-1', 'location-1')"
        )

        migration = _load_migration(connection)
        migration.upgrade()

        row = connection.exec_driver_sql(
            "SELECT scene_id, location_id FROM scene_location_links"
        ).one()
        assert row == ("scene-1", "location-1")
        indexes = {index["name"] for index in sa.inspect(connection).get_indexes("scene_location_links")}
        assert "ix_scene_location_links_location_id" in indexes


def test_upgrade_still_creates_table_on_clean_database() -> None:
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        _create_parent_tables(connection)

        migration = _load_migration(connection)
        migration.upgrade()

        inspector = sa.inspect(connection)
        assert inspector.has_table("scene_location_links")
        assert set(inspector.get_pk_constraint("scene_location_links")["constrained_columns"]) == {
            "scene_id"
        }
        indexes = {index["name"] for index in inspector.get_indexes("scene_location_links")}
        assert "ix_scene_location_links_location_id" in indexes


def test_upgrade_refuses_to_adopt_incompatible_existing_table() -> None:
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        _create_parent_tables(connection)
        connection.exec_driver_sql(
            "CREATE TABLE scene_location_links (scene_id VARCHAR(36) PRIMARY KEY)"
        )

        migration = _load_migration(connection)

        with pytest.raises(RuntimeError, match="incompatible columns"):
            migration.upgrade()
