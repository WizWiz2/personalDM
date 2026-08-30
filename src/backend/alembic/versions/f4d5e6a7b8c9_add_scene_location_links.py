"""Add authoritative structured locations to scenes.

Revision ID: f4d5e6a7b8c9
Revises: e3c4d5e6f7a8
Create Date: 2026-08-01
"""

from alembic import op
import sqlalchemy as sa


revision = "f4d5e6a7b8c9"
down_revision = "e3c4d5e6f7a8"
branch_labels = None
depends_on = None

_TABLE = "scene_location_links"
_INDEX = "ix_scene_location_links_location_id"


def _validate_existing_table(bind) -> None:
    """Adopt only the exact legacy shape this migration would have created."""
    inspector = sa.inspect(bind)
    columns = {column["name"]: column for column in inspector.get_columns(_TABLE)}
    expected_columns = {"scene_id", "location_id"}
    if set(columns) != expected_columns:
        raise RuntimeError(
            f"existing {_TABLE} has incompatible columns: "
            f"expected {sorted(expected_columns)}, got {sorted(columns)}"
        )

    primary_key = set(inspector.get_pk_constraint(_TABLE).get("constrained_columns") or [])
    if primary_key != {"scene_id"}:
        raise RuntimeError(
            f"existing {_TABLE} has incompatible primary key: {sorted(primary_key)}"
        )

    if columns["location_id"].get("nullable", True):
        raise RuntimeError(f"existing {_TABLE}.location_id must be NOT NULL")

    foreign_keys = {
        (
            tuple(foreign_key.get("constrained_columns") or []),
            foreign_key.get("referred_table"),
            tuple(foreign_key.get("referred_columns") or []),
        )
        for foreign_key in inspector.get_foreign_keys(_TABLE)
    }
    required_foreign_keys = {
        (("scene_id",), "scenes", ("id",)),
        (("location_id",), "entities", ("id",)),
    }
    if not required_foreign_keys.issubset(foreign_keys):
        raise RuntimeError(
            f"existing {_TABLE} has incompatible foreign keys: {sorted(foreign_keys, key=str)}"
        )

    named_index = next(
        (index for index in inspector.get_indexes(_TABLE) if index.get("name") == _INDEX),
        None,
    )
    if named_index is not None and named_index.get("column_names") != ["location_id"]:
        raise RuntimeError(
            f"existing {_INDEX} has incompatible columns: {named_index.get('column_names')}"
        )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table(_TABLE):
        # Some pre-migration developer/user databases already contain this ORM table while their
        # alembic_version is still e3c4d5e6f7a8. Preserve the data, but only when the physical table
        # is exactly compatible with the schema this revision owns.
        _validate_existing_table(bind)
        index_names = {index.get("name") for index in sa.inspect(bind).get_indexes(_TABLE)}
        if _INDEX not in index_names:
            op.create_index(_INDEX, _TABLE, ["location_id"], unique=False)
        return

    op.create_table(
        _TABLE,
        sa.Column("scene_id", sa.String(length=36), nullable=False),
        sa.Column("location_id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["scene_id"],
            ["scenes.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["location_id"],
            ["entities.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("scene_id"),
    )
    op.create_index(
        _INDEX,
        _TABLE,
        ["location_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        _INDEX,
        table_name=_TABLE,
    )
    op.drop_table(_TABLE)
