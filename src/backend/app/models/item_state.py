from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ItemStateSnapshot(BaseModel):
    item_id: UUID
    name: str
    owner_id: UUID | None = None
    owner_name: str | None = None
    location_id: UUID | None = None
    location_name: str | None = None
    placement_detail: str | None = None
    quantity: int | None = None  # typed count of units; wins over any count in the name
    owner_location_id: UUID | None = None
    owner_location_name: str | None = None
    revealed_content: str | None = None
    content_source_step_id: UUID | None = None
    properties: dict[str, str] = Field(default_factory=dict)


class ItemContentRevelation(BaseModel):
    """A discovery bound to one existing item and one successfully executed act."""
    item_id: UUID
    action_index: int = Field(ge=0, le=7)
    content: str = Field(min_length=1, max_length=2500)


class RequestedInventoryAction(BaseModel):
    """Frozen request to another actor; execution still requires their resolved consent."""
    model_config = ConfigDict(extra="forbid")
    actor_id: UUID
    item_id: UUID | None = None
    produced_item_action_index: int | None = Field(default=None, ge=0, le=7)
    produced_item_ordinal: int | None = Field(default=None, ge=0, le=3)
    operation: Literal["take", "drop", "give", "place", "reposition", "store"]
    target_id: UUID | None = None
    placement_detail: str | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def validate_operation(self):
        if self.produced_item_ordinal is not None and self.produced_item_action_index is None:
            raise ValueError("a produced ordinal requires its creation action")
        if (self.item_id is None) == (self.produced_item_action_index is None):
            raise ValueError("requested inventory must select one existing or produced identity")
        if self.operation == "give" and self.target_id is None:
            raise ValueError("requested give requires a recipient")
        if self.operation == "reposition" and not self.placement_detail:
            raise ValueError("requested reposition requires exact placement")
        return self


class ItemPropertyChange(BaseModel):
    """A physical property result, not custody, identity or document contents."""
    model_config = ConfigDict(extra="forbid")
    item_id: UUID
    action_index: int = Field(ge=0, le=7)
    property_name: str = Field(min_length=1, max_length=120)
    value: str = Field(min_length=1, max_length=1000)


class ItemPositionClaim(BaseModel):
    model_config = ConfigDict(json_schema_extra={"required": [
        "item_id", "evidence", "scope", "aspect", "action_step_index", "moment",
        "mode", "owner_id", "location_id", "placement_conflicts", "placement_matches", "polarity",
    ]})
    item_id: UUID
    evidence: str = Field(min_length=1, max_length=1000)
    scope: Literal["current", "past", "speech", "conditional", "executed_action"]
    aspect: Literal["custody", "placement", "custody_and_placement"] = "custody_and_placement"
    action_step_index: int | None = Field(default=None, ge=0, le=15)
    moment: Literal["before", "after"] = "after"
    polarity: Literal["asserted", "denied"] = "asserted"
    mode: Literal["owned", "located", "unpositioned"]
    owner_id: UUID | None = None
    location_id: UUID | None = None
    placement_conflicts: bool = False
    placement_matches: bool | None = None


