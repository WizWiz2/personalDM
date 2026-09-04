from __future__ import annotations

from enum import Enum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class TruthEffectType(str, Enum):
    """Small engine protocol, not a domain vocabulary."""

    SET_FLUENT = "set_fluent"
    ADD_RELATION = "add_relation"
    REMOVE_RELATION = "remove_relation"
    RECORD_MENTION = "record_mention"


class TruthEventEffectCreate(BaseModel):
    effect_type: TruthEffectType
    payload: dict[str, Any]


class TruthEventEvidenceCreate(BaseModel):
    evidence_type: str
    content: str | None = None
    source_ref: str | None = None
    source_turn_id: UUID | None = None


class CanonicalEventCreate(BaseModel):
    event_key: str = Field(min_length=1, max_length=255)
    event_type: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1)
    source_kind: str = Field(min_length=1, max_length=64)
    source_turn_id: UUID | None = None
    world_time: str | None = None
    location_id: UUID | None = None
    importance: str = "normal"
    participant_ids: list[UUID] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)
    effects: list[TruthEventEffectCreate] = Field(default_factory=list)
    evidence: list[TruthEventEvidenceCreate] = Field(default_factory=list)


class CanonicalEventRead(BaseModel):
    event_id: UUID
    campaign_id: UUID
    sequence: int
    event_key: str
    event_type: str
    description: str
    source_kind: str
    source_turn_id: UUID | None
    status: str
    payload: dict[str, Any]


class SemanticTypeCreate(BaseModel):
    kind: str = Field(min_length=1, max_length=32)
    canonical_label: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1)
    cardinality: str = "single"
    value_schema: dict[str, Any] | None = None
    created_by_event_id: UUID | None = None
    system_key: str | None = Field(default=None, min_length=1, max_length=128)


class EntityResolutionCandidate(BaseModel):
    entity_id: UUID
    entity_type: str
    canonical_name: str
    description: str | None = None
    scene_local: bool = False
    context_linked: bool = False
    prior_mention_count: int = 0


class SemanticTypeResolutionCandidate(BaseModel):
    semantic_type_id: UUID
    kind: str
    canonical_label: str
    description: str
    cardinality: Literal["single", "multi"]
    value_schema: dict[str, Any] | None = None
    active_for_subject: bool = False


class EntityResolutionDecision(BaseModel):
    """A model may select one supplied entity ID or explicitly request a new entity."""

    decision: Literal["existing", "new"]
    entity_id: UUID | None = None

    @model_validator(mode="after")
    def validate_choice(self):
        if self.decision == "existing" and self.entity_id is None:
            raise ValueError("existing entity resolution requires entity_id")
        if self.decision == "new" and self.entity_id is not None:
            raise ValueError("new entity resolution cannot carry entity_id")
        return self


class NewSemanticTypeDraft(BaseModel):
    canonical_label: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1, max_length=1200)
    cardinality: Literal["single", "multi"] = "single"
    value_schema: dict[str, Any] | None = None


class SemanticTypeResolutionDecision(BaseModel):
    """A model may select one supplied semantic ID or define one genuinely new slot."""

    decision: Literal["existing", "new"]
    semantic_type_id: UUID | None = None
    new_type: NewSemanticTypeDraft | None = None

    @model_validator(mode="after")
    def validate_choice(self):
        if self.decision == "existing":
            if self.semantic_type_id is None:
                raise ValueError("existing semantic resolution requires semantic_type_id")
            if self.new_type is not None:
                raise ValueError("existing semantic resolution cannot define new_type")
        else:
            if self.semantic_type_id is not None:
                raise ValueError("new semantic resolution cannot carry semantic_type_id")
            if self.new_type is None:
                raise ValueError("new semantic resolution requires new_type")
        return self


class EntityMentionObservation(BaseModel):
    """One linguistic entity reference before stable identity is chosen."""

    observation_key: str = Field(min_length=1, max_length=255)
    mention_text: str = Field(min_length=1, max_length=500)
    entity_type: str = Field(min_length=1, max_length=50)
    description: str | None = Field(default=None, max_length=1600)
    source_turn_id: UUID | None = None
    scene_id: UUID | None = None
    mention_kind: str | None = Field(default=None, max_length=64)
    evidence: str | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    context_entity_ids: list[UUID] = Field(default_factory=list, max_length=16)


class FluentObservation(BaseModel):
    """One evidence-backed state observation after entity identity is resolved."""

    observation_key: str = Field(min_length=1, max_length=255)
    subject_entity_id: UUID
    semantic_description: str = Field(min_length=1, max_length=1200)
    value: Any
    description: str = Field(min_length=1, max_length=1600)
    source_turn_id: UUID | None = None
    scene_id: UUID | None = None
    authority: str = Field(default="semantic_compiler", min_length=1, max_length=64)
    evidence: str | None = None
    cardinality_hint: Literal["single", "multi"] | None = None


class RelationObservation(BaseModel):
    """One evidence-backed relation observation between already resolved entities."""

    observation_key: str = Field(min_length=1, max_length=255)
    subject_entity_id: UUID
    object_entity_id: UUID
    semantic_description: str = Field(min_length=1, max_length=1200)
    present: bool = True
    description: str = Field(min_length=1, max_length=1600)
    source_turn_id: UUID | None = None
    authority: str = Field(default="semantic_compiler", min_length=1, max_length=64)
    evidence: str | None = None
    cardinality_hint: Literal["single", "multi"] | None = None


class WorldReductionResult(BaseModel):
    event_id: UUID
    applied_effects: int = 0
    skipped_effects: int = 0


class WorldRebuildResult(BaseModel):
    replayed_events: int = 0
    applied_effects: int = 0
