"""Add Truth Engine 2 canonical event and temporal projection foundation."""

from alembic import op
import sqlalchemy as sa


revision = "a7b8c9d0e1f2"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None


ENTITY_NAME_UNIQUE = "uq_campaign_entity_name"


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _column_names(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {column["name"] for column in inspector.get_columns(table_name)}


def _index_names(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {index["name"] for index in inspector.get_indexes(table_name)}


def _unique_constraint_names(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {
        constraint["name"]
        for constraint in inspector.get_unique_constraints(table_name)
        if constraint.get("name")
    }


def _ensure_index(table_name: str, index_name: str, columns: list[str]) -> None:
    if index_name not in _index_names(table_name):
        op.create_index(index_name, table_name, columns)


def _drop_legacy_entity_name_identity_constraint(tables: set[str]) -> None:
    if "entities" not in tables:
        return
    if ENTITY_NAME_UNIQUE not in _unique_constraint_names("entities"):
        return
    # SQLite cannot ALTER a table-level UNIQUE constraint directly. Alembic batch mode
    # recreates the table while preserving rows/FKs and drops only this legacy identity rule.
    with op.batch_alter_table("entities", recreate="always") as batch_op:
        batch_op.drop_constraint(ENTITY_NAME_UNIQUE, type_="unique")


def _restore_legacy_entity_name_identity_constraint(tables: set[str]) -> None:
    if "entities" not in tables:
        return
    if ENTITY_NAME_UNIQUE in _unique_constraint_names("entities"):
        return
    duplicate = op.get_bind().execute(
        sa.text(
            """
            SELECT campaign_id, entity_type, canonical_name
            FROM entities
            GROUP BY campaign_id, entity_type, canonical_name
            HAVING COUNT(*) > 1
            LIMIT 1
            """
        )
    ).first()
    if duplicate is not None:
        raise RuntimeError(
            "Cannot downgrade Truth Engine 2: distinct entities now share a display label; "
            "restoring uq_campaign_entity_name would destroy valid identity data."
        )
    with op.batch_alter_table("entities", recreate="always") as batch_op:
        batch_op.create_unique_constraint(
            ENTITY_NAME_UNIQUE,
            ["campaign_id", "entity_type", "canonical_name"],
        )


def upgrade() -> None:
    tables = _table_names()
    _drop_legacy_entity_name_identity_constraint(tables)

    if "truth_event_records" not in tables:
        op.create_table(
            "truth_event_records",
            sa.Column("event_id", sa.String(length=36), nullable=False),
            sa.Column("campaign_id", sa.String(length=36), nullable=False),
            sa.Column("sequence", sa.Integer(), nullable=False),
            sa.Column("event_key", sa.String(length=255), nullable=False),
            sa.Column("source_kind", sa.String(length=64), nullable=False),
            sa.Column("source_turn_id", sa.String(length=36), nullable=True),
            sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("status", sa.String(length=24), nullable=False, server_default="active"),
            sa.Column("reverted_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["source_turn_id"], ["turns.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("event_id"),
            sa.UniqueConstraint("campaign_id", "event_key", name="uq_truth_event_campaign_key"),
            sa.UniqueConstraint(
                "campaign_id", "sequence", name="uq_truth_event_campaign_sequence"
            ),
        )
        tables.add("truth_event_records")
    _ensure_index("truth_event_records", "ix_truth_event_records_campaign_id", ["campaign_id"])
    _ensure_index(
        "truth_event_records", "ix_truth_event_records_source_turn_id", ["source_turn_id"]
    )
    _ensure_index("truth_event_records", "ix_truth_event_records_status", ["status"])

    if "truth_event_effects" not in tables:
        op.create_table(
            "truth_event_effects",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("event_id", sa.String(length=36), nullable=False),
            sa.Column("effect_index", sa.Integer(), nullable=False),
            sa.Column("effect_type", sa.String(length=64), nullable=False),
            sa.Column("payload_json", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("event_id", "effect_index", name="uq_truth_event_effect_index"),
        )
        tables.add("truth_event_effects")
    _ensure_index("truth_event_effects", "ix_truth_event_effects_event_id", ["event_id"])

    if "truth_event_evidence" not in tables:
        op.create_table(
            "truth_event_evidence",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("event_id", sa.String(length=36), nullable=False),
            sa.Column("evidence_type", sa.String(length=64), nullable=False),
            sa.Column("source_turn_id", sa.String(length=36), nullable=True),
            sa.Column("source_ref", sa.String(length=255), nullable=True),
            sa.Column("content", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["source_turn_id"], ["turns.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )
        tables.add("truth_event_evidence")
    _ensure_index("truth_event_evidence", "ix_truth_event_evidence_event_id", ["event_id"])
    _ensure_index(
        "truth_event_evidence", "ix_truth_event_evidence_source_turn_id", ["source_turn_id"]
    )

    if "semantic_types" not in tables:
        op.create_table(
            "semantic_types",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("campaign_id", sa.String(length=36), nullable=False),
            sa.Column("system_key", sa.String(length=128), nullable=True),
            sa.Column("kind", sa.String(length=32), nullable=False),
            sa.Column("canonical_label", sa.String(length=255), nullable=False),
            sa.Column("description", sa.Text(), nullable=False),
            sa.Column("cardinality", sa.String(length=16), nullable=False, server_default="single"),
            sa.Column("value_schema_json", sa.Text(), nullable=True),
            sa.Column("created_by_event_id", sa.String(length=36), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["created_by_event_id"], ["events.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )
        tables.add("semantic_types")
    elif "system_key" not in _column_names("semantic_types"):
        op.add_column(
            "semantic_types",
            sa.Column("system_key", sa.String(length=128), nullable=True),
        )
    _ensure_index("semantic_types", "ix_semantic_types_campaign_id", ["campaign_id"])
    _ensure_index("semantic_types", "ix_semantic_types_kind", ["kind"])
    if "uq_semantic_type_campaign_system_key" not in _index_names("semantic_types"):
        op.create_index(
            "uq_semantic_type_campaign_system_key",
            "semantic_types",
            ["campaign_id", "system_key"],
            unique=True,
        )

    if "fluent_assertions" not in tables:
        op.create_table(
            "fluent_assertions",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("campaign_id", sa.String(length=36), nullable=False),
            sa.Column("subject_entity_id", sa.String(length=36), nullable=False),
            sa.Column("semantic_type_id", sa.String(length=36), nullable=False),
            sa.Column("value_json", sa.Text(), nullable=False),
            sa.Column("scene_id", sa.String(length=36), nullable=True),
            sa.Column("valid_from_event_id", sa.String(length=36), nullable=False),
            sa.Column("valid_until_event_id", sa.String(length=36), nullable=True),
            sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("authority", sa.String(length=64), nullable=False),
            sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["scene_id"], ["scenes.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["semantic_type_id"], ["semantic_types.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["subject_entity_id"], ["entities.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["valid_from_event_id"], ["events.id"], ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["valid_until_event_id"], ["events.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )
        tables.add("fluent_assertions")
    for column in (
        "campaign_id",
        "subject_entity_id",
        "semantic_type_id",
        "scene_id",
        "valid_from_event_id",
        "valid_until_event_id",
        "is_current",
    ):
        _ensure_index(
            "fluent_assertions", f"ix_fluent_assertions_{column}", [column]
        )

    if "world_relation_assertions" not in tables:
        op.create_table(
            "world_relation_assertions",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("campaign_id", sa.String(length=36), nullable=False),
            sa.Column("subject_entity_id", sa.String(length=36), nullable=False),
            sa.Column("semantic_type_id", sa.String(length=36), nullable=False),
            sa.Column("object_entity_id", sa.String(length=36), nullable=False),
            sa.Column("valid_from_event_id", sa.String(length=36), nullable=False),
            sa.Column("valid_until_event_id", sa.String(length=36), nullable=True),
            sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("authority", sa.String(length=64), nullable=False),
            sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["object_entity_id"], ["entities.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["semantic_type_id"], ["semantic_types.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["subject_entity_id"], ["entities.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["valid_from_event_id"], ["events.id"], ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["valid_until_event_id"], ["events.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )
        tables.add("world_relation_assertions")
    for column in (
        "campaign_id",
        "subject_entity_id",
        "semantic_type_id",
        "object_entity_id",
        "valid_from_event_id",
        "valid_until_event_id",
        "is_current",
    ):
        _ensure_index(
            "world_relation_assertions",
            f"ix_world_relation_assertions_{column}",
            [column],
        )

    if "entity_mentions" not in tables:
        op.create_table(
            "entity_mentions",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("campaign_id", sa.String(length=36), nullable=False),
            sa.Column("entity_id", sa.String(length=36), nullable=True),
            sa.Column("mention_text", sa.Text(), nullable=False),
            sa.Column("mention_kind", sa.String(length=64), nullable=True),
            sa.Column("source_event_id", sa.String(length=36), nullable=False),
            sa.Column("source_turn_id", sa.String(length=36), nullable=True),
            sa.Column("scene_id", sa.String(length=36), nullable=True),
            sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
            sa.Column(
                "resolver_kind", sa.String(length=64), nullable=False, server_default="structured"
            ),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["entity_id"], ["entities.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["scene_id"], ["scenes.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["source_event_id"], ["events.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["source_turn_id"], ["turns.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )
        tables.add("entity_mentions")
    for column in ("campaign_id", "entity_id", "source_event_id", "source_turn_id", "scene_id"):
        _ensure_index("entity_mentions", f"ix_entity_mentions_{column}", [column])

    if "assertion_support" not in tables:
        op.create_table(
            "assertion_support",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("campaign_id", sa.String(length=36), nullable=False),
            sa.Column("assertion_kind", sa.String(length=32), nullable=False),
            sa.Column("assertion_id", sa.String(length=36), nullable=False),
            sa.Column("event_id", sa.String(length=36), nullable=False),
            sa.Column("evidence_id", sa.String(length=36), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["evidence_id"], ["truth_event_evidence.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "assertion_kind", "assertion_id", "event_id", name="uq_assertion_support_event"
            ),
        )
        tables.add("assertion_support")
    _ensure_index("assertion_support", "ix_assertion_support_campaign_id", ["campaign_id"])
    _ensure_index("assertion_support", "ix_assertion_support_assertion_id", ["assertion_id"])
    _ensure_index("assertion_support", "ix_assertion_support_event_id", ["event_id"])

    if "truth_effect_applications" not in tables:
        op.create_table(
            "truth_effect_applications",
            sa.Column("effect_id", sa.String(length=36), nullable=False),
            sa.Column("applied_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["effect_id"], ["truth_event_effects.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("effect_id"),
        )
        tables.add("truth_effect_applications")

    if "truth_projection_state" not in tables:
        op.create_table(
            "truth_projection_state",
            sa.Column("campaign_id", sa.String(length=36), nullable=False),
            sa.Column("last_applied_sequence", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("campaign_id"),
        )


def downgrade() -> None:
    tables = _table_names()
    # Refuse an unsafe downgrade before deleting TE2 tables. Once duplicate labels represent
    # distinct UUID identities, the old uniqueness rule cannot be restored without data loss.
    _restore_legacy_entity_name_identity_constraint(tables)
    for table_name in (
        "truth_projection_state",
        "truth_effect_applications",
        "assertion_support",
        "entity_mentions",
        "world_relation_assertions",
        "fluent_assertions",
        "semantic_types",
        "truth_event_evidence",
        "truth_event_effects",
        "truth_event_records",
    ):
        if table_name in tables:
            op.drop_table(table_name)
