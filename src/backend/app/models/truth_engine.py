from __future__ import annotations

from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


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


class WorldReductionResult(BaseModel):
    event_id: UUID
    applied_effects: int = 0
    skipped_effects: int = 0


class WorldRebuildResult(BaseModel):
    replayed_events: int = 0
    applied_effects: int = 0
