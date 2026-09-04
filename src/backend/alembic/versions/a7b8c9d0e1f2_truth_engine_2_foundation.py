"""Add Truth Engine 2 canonical event and temporal projection foundation."""

from alembic import op
import sqlalchemy as sa


revision = "a7b8c9d0e1f2"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
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
        sa.UniqueConstraint("campaign_id", "sequence", name="uq_truth_event_campaign_sequence"),
    )
    op.create_index("ix_truth_event_records_campaign_id", "truth_event_records", ["campaign_id"])
    op.create_index("ix_truth_event_records_source_turn_id", "truth_event_records", ["source_turn_id"])
    op.create_index("ix_truth_event_records_status", "truth_event_records", ["status"])

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
    op.create_index("ix_truth_event_effects_event_id", "truth_event_effects", ["event_id"])

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
    op.create_index("ix_truth_event_evidence_event_id", "truth_event_evidence", ["event_id"])
    op.create_index(
        "ix_truth_event_evidence_source_turn_id", "truth_event_evidence", ["source_turn_id"]
    )

    op.create_table(
        "semantic_types",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("campaign_id", sa.String(length=36), nullable=False),
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
    op.create_index("ix_semantic_types_campaign_id", "semantic_types", ["campaign_id"])
    op.create_index("ix_semantic_types_kind", "semantic_types", ["kind"])

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
    for column in (
        "campaign_id",
        "subject_entity_id",
        "semantic_type_id",
        "scene_id",
        "valid_from_event_id",
        "valid_until_event_id",
        "is_current",
    ):
        op.create_index(f"ix_fluent_assertions_{column}", "fluent_assertions", [column])

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
    for column in (
        "campaign_id",
        "subject_entity_id",
        "semantic_type_id",
        "object_entity_id",
        "valid_from_event_id",
        "valid_until_event_id",
        "is_current",
    ):
        op.create_index(
            f"ix_world_relation_assertions_{column}", "world_relation_assertions", [column]
        )

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
        sa.Column("resolver_kind", sa.String(length=64), nullable=False, server_default="structured"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["entity_id"], ["entities.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["scene_id"], ["scenes.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_event_id"], ["events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_turn_id"], ["turns.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("campaign_id", "entity_id", "source_event_id", "source_turn_id", "scene_id"):
        op.create_index(f"ix_entity_mentions_{column}", "entity_mentions", [column])

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
    op.create_index("ix_assertion_support_campaign_id", "assertion_support", ["campaign_id"])
    op.create_index("ix_assertion_support_assertion_id", "assertion_support", ["assertion_id"])
    op.create_index("ix_assertion_support_event_id", "assertion_support", ["event_id"])

    op.create_table(
        "truth_effect_applications",
        sa.Column("effect_id", sa.String(length=36), nullable=False),
        sa.Column("applied_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["effect_id"], ["truth_event_effects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("effect_id"),
    )

    op.create_table(
        "truth_projection_state",
        sa.Column("campaign_id", sa.String(length=36), nullable=False),
        sa.Column("last_applied_sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("campaign_id"),
    )


def downgrade() -> None:
    op.drop_table("truth_projection_state")
    op.drop_table("truth_effect_applications")
    op.drop_index("ix_assertion_support_event_id", table_name="assertion_support")
    op.drop_index("ix_assertion_support_assertion_id", table_name="assertion_support")
    op.drop_index("ix_assertion_support_campaign_id", table_name="assertion_support")
    op.drop_table("assertion_support")
    for column in reversed(("campaign_id", "entity_id", "source_event_id", "source_turn_id", "scene_id")):
        op.drop_index(f"ix_entity_mentions_{column}", table_name="entity_mentions")
    op.drop_table("entity_mentions")
    for column in reversed(
        (
            "campaign_id",
            "subject_entity_id",
            "semantic_type_id",
            "object_entity_id",
            "valid_from_event_id",
            "valid_until_event_id",
            "is_current",
        )
    ):
        op.drop_index(f"ix_world_relation_assertions_{column}", table_name="world_relation_assertions")
    op.drop_table("world_relation_assertions")
    for column in reversed(
        (
            "campaign_id",
            "subject_entity_id",
            "semantic_type_id",
            "scene_id",
            "valid_from_event_id",
            "valid_until_event_id",
            "is_current",
        )
    ):
        op.drop_index(f"ix_fluent_assertions_{column}", table_name="fluent_assertions")
    op.drop_table("fluent_assertions")
    op.drop_index("ix_semantic_types_kind", table_name="semantic_types")
    op.drop_index("ix_semantic_types_campaign_id", table_name="semantic_types")
    op.drop_table("semantic_types")
    op.drop_index("ix_truth_event_evidence_source_turn_id", table_name="truth_event_evidence")
    op.drop_index("ix_truth_event_evidence_event_id", table_name="truth_event_evidence")
    op.drop_table("truth_event_evidence")
    op.drop_index("ix_truth_event_effects_event_id", table_name="truth_event_effects")
    op.drop_table("truth_event_effects")
    op.drop_index("ix_truth_event_records_status", table_name="truth_event_records")
    op.drop_index("ix_truth_event_records_source_turn_id", table_name="truth_event_records")
    op.drop_index("ix_truth_event_records_campaign_id", table_name="truth_event_records")
    op.drop_table("truth_event_records")
