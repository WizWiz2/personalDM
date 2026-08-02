from uuid import UUID

from pydantic import BaseModel, Field

from app.models.belief import BeliefRead
from app.models.character import CharacterRead, CharacterUpdate
from app.models.entity import EntityRead
from app.models.goal import GoalRead
from app.models.relationship import RelationshipRead


class CharacterEquipmentRead(BaseModel):
    id: UUID
    canonical_name: str
    description: str | None = None
    item_type: str | None = None
    physical_properties: str | None = None
    magical_properties: str | None = None
    value_estimate: str | None = None
    current_owner_id: UUID | None = None
    current_location_id: UUID | None = None
    is_unique: bool = False
    lore: str | None = None


class CharacterCardRead(BaseModel):
    character: CharacterRead
    current_location: EntityRead | None = None
    goals: list[GoalRead] = Field(default_factory=list)
    beliefs: list[BeliefRead] = Field(default_factory=list)
    relationships: list[RelationshipRead] = Field(default_factory=list)
    equipment: list[CharacterEquipmentRead] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    identity: dict = Field(default_factory=dict)
    mechanics: dict = Field(default_factory=dict)
    resources: dict = Field(default_factory=dict)
    social: dict = Field(default_factory=dict)
    visibility: dict = Field(default_factory=dict)
    missing_fields: list[str] = Field(default_factory=list)
    completion_ratio: float = 0.0
    ready_for_play: bool = False


class CharacterCardUpdate(CharacterUpdate):
    """One-payload editor for the durable character card.

    Core fields are inherited from CharacterUpdate. Structured extension sections are
    merged into Entity.custom_fields instead of replacing unrelated metadata.
    Goals, beliefs, relationships and equipment retain their dedicated APIs because
    they have independent provenance and lifecycle.
    """

    capabilities: list[str] | None = None
    limitations: list[str] | None = None
    identity: dict | None = None
    mechanics: dict | None = None
    resources: dict | None = None
    social: dict | None = None
    visibility: dict | None = None
