"""Model-facing canon deltas with a payload contract for each change domain.

The storage envelope remains permissive for legacy reads. Generation uses this
discriminated union so a free-form payload cannot satisfy a fact or movement.
Evidence and entity identity are still checked by the canon pipeline.
"""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.services.canon_semantics import CanonOperation, FactCardinality, OutcomeAtom, normalize_key


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FactPayload(_Payload):
    subject: str = Field(min_length=1, description="State-bearing subject; free text, not an entity ID.")
    predicate: str = Field(min_length=1)
    object_value: str | None = Field(description="Confirmed value; null only for retraction.")
    truth_status: Literal["true", "false", "disputed"] = "true"
    visibility: Literal["dm", "public"] = "public"
    scope: Literal["scene", "campaign"] = "scene"
    memory_kind: Literal["world_canon", "entity_state", "scene_state"] | None = None


class EventPayload(_Payload):
    event_type: str = Field(min_length=1)
    description: str = Field(min_length=3)
    participant_ids: list[str] = Field(min_length=1, description="Exact catalog identities of participating characters. World state without a participating character belongs in a fact, not an actor event.")
    location_id: str | None = None


class MovementPayload(_Payload):
    character_id: str = Field(min_length=1, description="Exact catalog character identity.")
    location_id: str = Field(min_length=1, description="Exact catalog location identity.")
    description: str = Field(min_length=3)


class RelationshipPayload(_Payload):
    subject_id: str = Field(min_length=1)
    object_id: str = Field(min_length=1)
    relation_type: str = Field(min_length=1)
    description: str = Field(min_length=3)
    reason: str | None = None
    intensity: float = Field(default=0.0, ge=0.0, le=1.0)


class KnowledgePayload(_Payload):
    recipient_id: str = Field(min_length=1)
    proposition: str = Field(min_length=3)
    source_character_id: str | None = None
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    status: Literal["known", "believed", "doubted"] = "known"
    previous_proposition: str | None = None


class ItemTransferPayload(_Payload):
    item_id: str = Field(min_length=1)
    owner_id: str | None
    location_id: str | None
    description: str = Field(min_length=3)

    @model_validator(mode="after")
    def single_destination(self):
        if bool(self.owner_id) == bool(self.location_id):
            raise ValueError("Item transfer needs exactly one owner or location")
        return self


class NarrativeDetailPayload(_Payload):
    text: str = Field(min_length=1, description="Exact evidence quote from the published narrative.")
    detail_type: Literal["gaze", "pose", "expression", "gesture", "ambient", "sensory", "spatial", "other"]
    subject_entity_id: str | None = None
    visibility: Literal["dm", "public"] = "public"
    turn_window: int = Field(default=3, ge=1, le=12)


class _Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome_id: str = Field(min_length=1)
    operation: CanonOperation = CanonOperation.ASSERT
    cardinality: FactCardinality = FactCardinality.SINGLE


class FactProposal(_Proposal):
    change_type: Literal["fact"]
    payload: FactPayload

    @model_validator(mode="after")
    def confirmed_value(self):
        if self.operation != CanonOperation.RETRACT and not (self.payload.object_value or "").strip():
            raise ValueError("Non-retracted fact requires a confirmed object_value")
        return self


class EventProposal(_Proposal):
    change_type: Literal["event"]
    payload: EventPayload


class MovementProposal(_Proposal):
    change_type: Literal["movement"]
    payload: MovementPayload


class RelationshipProposal(_Proposal):
    change_type: Literal["relationship"]
    payload: RelationshipPayload


class KnowledgeProposal(_Proposal):
    change_type: Literal["knowledge"]
    payload: KnowledgePayload


class ItemTransferProposal(_Proposal):
    change_type: Literal["item_transfer"]
    payload: ItemTransferPayload


class NarrativeDetailProposal(_Proposal):
    change_type: Literal["narrative_detail"]
    payload: NarrativeDetailPayload


ScribeProposal = Annotated[
    FactProposal | EventProposal | MovementProposal | RelationshipProposal
    | KnowledgeProposal | ItemTransferProposal | NarrativeDetailProposal,
    Field(discriminator="change_type"),
]


class CanonEnvelopeWire(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcomes: list[OutcomeAtom] = Field(max_length=10)
    proposals: list[ScribeProposal] = Field(max_length=12)

    @model_validator(mode="after")
    def complete_durable_deltas(self):
        outcome_ids = {normalize_key(item.id) for item in self.outcomes}
        if len(outcome_ids) != len(self.outcomes):
            raise ValueError("Outcome IDs must be unique")
        proposal_ids = {normalize_key(item.outcome_id) for item in self.proposals}
        if proposal_ids - outcome_ids:
            raise ValueError("Every proposal must reference an outcome in this envelope")
        missing = {normalize_key(item.id) for item in self.outcomes if item.durable} - proposal_ids
        if missing:
            raise ValueError("Durable outcomes require typed deltas: " + ", ".join(sorted(missing)))
        return self


class FactEnvelopeWire(CanonEnvelopeWire):
    """Executed interaction recovery may propose only objective facts."""

    proposals: list[FactProposal] = Field(max_length=12)
