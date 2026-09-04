from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.engine import Base
from app.db.tables import generate_uuid


class TruthEventRecord(Base):
    """Canonical-event metadata layered onto the existing Event row.

    The legacy `events` table remains the shared event representation. A row in this table marks the
    corresponding event as part of the Truth Engine 2 immutable log and gives it deterministic
    ordering/idempotency metadata.
    """

    __tablename__ = "truth_event_records"
    __table_args__ = (
        UniqueConstraint("campaign_id", "sequence", name="uq_truth_event_campaign_sequence"),
        UniqueConstraint("campaign_id", "event_key", name="uq_truth_event_campaign_key"),
    )

    event_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("events.id", ondelete="CASCADE"), primary_key=True
    )
    campaign_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_key: Mapped[str] = mapped_column(String(255), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    source_turn_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("turns.id", ondelete="SET NULL"), nullable=True, index=True
    )
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active", index=True)
    reverted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class TruthEventEffect(Base):
    """Immutable normalized effects owned by one canonical event."""

    __tablename__ = "truth_event_effects"
    __table_args__ = (
        UniqueConstraint("event_id", "effect_index", name="uq_truth_event_effect_index"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    event_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True
    )
    effect_index: Mapped[int] = mapped_column(Integer, nullable=False)
    effect_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class TruthEventEvidence(Base):
    """Normalized provenance for a canonical event."""

    __tablename__ = "truth_event_evidence"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    event_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True
    )
    evidence_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_turn_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("turns.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class SemanticType(Base):
    """Campaign-local semantic schema element.

    Identity is the UUID, not the label. There is deliberately no synonym/keyword list: candidate
    retrieval and semantic resolution operate on descriptions/embeddings. ``system_key`` is only
    for a tiny set of engine-owned protocol slots whose meaning is already known by structured
    executors; it is never used to recognize prose.
    """

    __tablename__ = "semantic_types"
    __table_args__ = (
        UniqueConstraint(
            "campaign_id",
            "system_key",
            name="uq_semantic_type_campaign_system_key",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    campaign_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    system_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    canonical_label: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    cardinality: Mapped[str] = mapped_column(String(16), nullable=False, default="single")
    value_schema_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_event_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("events.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class FluentAssertion(Base):
    """Temporal value of one semantic property for an entity."""

    __tablename__ = "fluent_assertions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    campaign_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    subject_entity_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("entities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    semantic_type_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("semantic_types.id", ondelete="CASCADE"), nullable=False, index=True
    )
    value_json: Mapped[str] = mapped_column(Text, nullable=False)
    scene_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("scenes.id", ondelete="SET NULL"), nullable=True, index=True
    )
    valid_from_event_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("events.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    valid_until_event_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("events.id", ondelete="SET NULL"), nullable=True, index=True
    )
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    authority: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class WorldRelationAssertion(Base):
    """Temporal graph edge between two stable entities."""

    __tablename__ = "world_relation_assertions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    campaign_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    subject_entity_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("entities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    semantic_type_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("semantic_types.id", ondelete="CASCADE"), nullable=False, index=True
    )
    object_entity_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("entities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    valid_from_event_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("events.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    valid_until_event_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("events.id", ondelete="SET NULL"), nullable=True, index=True
    )
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    authority: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class EntityMention(Base):
    """A textual mention linked to an entity without changing entity identity."""

    __tablename__ = "entity_mentions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    campaign_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    entity_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("entities.id", ondelete="SET NULL"), nullable=True, index=True
    )
    mention_text: Mapped[str] = mapped_column(Text, nullable=False)
    mention_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_event_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_turn_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("turns.id", ondelete="SET NULL"), nullable=True, index=True
    )
    scene_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("scenes.id", ondelete="SET NULL"), nullable=True, index=True
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    resolver_kind: Mapped[str] = mapped_column(String(64), nullable=False, default="structured")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class AssertionSupport(Base):
    """Provenance edge from a derived assertion to the event that supports it."""

    __tablename__ = "assertion_support"
    __table_args__ = (
        UniqueConstraint(
            "assertion_kind", "assertion_id", "event_id", name="uq_assertion_support_event"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    campaign_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    assertion_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    assertion_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    event_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True
    )
    evidence_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("truth_event_evidence.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class TruthEffectApplication(Base):
    """Projection bookkeeping; canonical effects themselves remain immutable."""

    __tablename__ = "truth_effect_applications"

    effect_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("truth_event_effects.id", ondelete="CASCADE"), primary_key=True
    )
    applied_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class TruthProjectionState(Base):
    __tablename__ = "truth_projection_state"

    campaign_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("campaigns.id", ondelete="CASCADE"), primary_key=True
    )
    last_applied_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )
