from __future__ import annotations

from collections.abc import Iterable

import sqlalchemy as sa
from alembic import op


ForeignKeySpec = tuple[tuple[str, ...], str, tuple[str, ...]]


def adopt_existing_table(
    table_name: str,
    *,
    required_columns: Iterable[str],
    primary_key: Iterable[str],
    foreign_keys: Iterable[ForeignKeySpec] = (),
    non_nullable: Iterable[str] = (),
    unique_constraints: Iterable[tuple[str, ...]] = (),
) -> bool:
    """Validate and adopt a pre-existing ORM table owned by an Alembic revision.

    Some early PersonalDM builds created newly-added ORM tables before the matching
    Alembic revision was stamped into ``alembic_version``. Existing user databases can
    therefore legitimately contain the physical table while Alembic still needs to run
    that revision. We preserve such data only when the existing table is structurally
    compatible with the revision. Unknown/incompatible shapes fail closed.

    Extra columns are allowed because a newer ORM definition may have already extended
    the table. Required revision-owned columns, keys and constraints must still exist.
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(table_name):
        return False

    columns = {column["name"]: column for column in inspector.get_columns(table_name)}
    required = set(required_columns)
    missing = sorted(required - set(columns))
    if missing:
        raise RuntimeError(
            f"existing {table_name} has incompatible columns; missing {missing}"
        )

    pk = set(inspector.get_pk_constraint(table_name).get("constrained_columns") or [])
    expected_pk = set(primary_key)
    if pk != expected_pk:
        raise RuntimeError(
            f"existing {table_name} has incompatible primary key: "
            f"expected {sorted(expected_pk)}, got {sorted(pk)}"
        )

    nullable_errors = sorted(
        column_name
        for column_name in set(non_nullable)
        if columns[column_name].get("nullable", True)
    )
    if nullable_errors:
        raise RuntimeError(
            f"existing {table_name} has nullable columns that must be NOT NULL: "
            f"{nullable_errors}"
        )

    existing_fks = {
        (
            tuple(foreign_key.get("constrained_columns") or []),
            str(foreign_key.get("referred_table") or ""),
            tuple(foreign_key.get("referred_columns") or []),
        )
        for foreign_key in inspector.get_foreign_keys(table_name)
    }
    required_fks = set(foreign_keys)
    if not required_fks.issubset(existing_fks):
        missing_fks = sorted(required_fks - existing_fks, key=str)
        raise RuntimeError(
            f"existing {table_name} has incompatible foreign keys; missing {missing_fks}"
        )

    existing_unique = {
        tuple(constraint.get("column_names") or [])
        for constraint in inspector.get_unique_constraints(table_name)
    }
    required_unique = set(unique_constraints)
    if not required_unique.issubset(existing_unique):
        missing_unique = sorted(required_unique - existing_unique, key=str)
        raise RuntimeError(
            f"existing {table_name} has incompatible unique constraints; "
            f"missing {missing_unique}"
        )

    return True


def ensure_index(
    table_name: str,
    index_name: str,
    columns: list[str],
    *,
    unique: bool = False,
) -> None:
    """Create one expected index when absent, validating same-name drift."""
    inspector = sa.inspect(op.get_bind())
    existing = next(
        (index for index in inspector.get_indexes(table_name) if index.get("name") == index_name),
        None,
    )
    if existing is None:
        op.create_index(index_name, table_name, columns, unique=unique)
        return

    if list(existing.get("column_names") or []) != columns or bool(
        existing.get("unique", False)
    ) != unique:
        raise RuntimeError(
            f"existing {index_name} is incompatible: "
            f"columns={existing.get('column_names')}, unique={existing.get('unique')}"
        )
