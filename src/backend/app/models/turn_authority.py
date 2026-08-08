from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PlannedNpcIntroduction(BaseModel):
    """One previously unknown NPC that this turn is allowed to introduce."""

    model_config = ConfigDict(extra="ignore")

    canonical_name: str = Field(min_length=2, max_length=120)
    role: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=800)
    appearance: str | None = Field(default=None, max_length=800)
    voice: str | None = Field(default=None, max_length=400)
    temporary_name: bool = False
    reason: str = Field(min_length=2, max_length=500)


class TurnAuthorityPlanExtension(BaseModel):
    """Fields every narrator turn must decide explicitly in addition to TurnPlan."""

    scene_disposition: Literal[
        "stay",
        "location_transition",
        "time_transition",
        "focus_transition",
        "sequence",
    ]
    npc_introductions: list[PlannedNpcIntroduction] = Field(
        default_factory=list,
        max_length=4,
    )


class TurnAuthority(BaseModel):
    """Single machine-readable source of truth shared by narrator and validator."""

    model_config = ConfigDict(extra="ignore")

    version: Literal[1] = 1
    campaign_id: UUID
    trigger_turn_id: UUID
    player_character_id: UUID | None = None
    player_character_name: str | None = None
    acting_character_id: UUID | None = None
    acting_character_name: str | None = None
    player_input: str

    source_scene_id: UUID | None = None
    target_scene_id: UUID | None = None
    scene_disposition: Literal[
        "stay",
        "location_transition",
        "time_transition",
        "focus_transition",
        "sequence",
        "actor_turn",
    ] = "stay"
    transition_type: str = "none"
    source_location_path: list[str] = Field(default_factory=list)
    target_location_path: list[str] = Field(default_factory=list)

    present_character_names: list[str] = Field(default_factory=list)
    known_absent_character_names: list[str] = Field(default_factory=list)
    allowed_new_npcs: list[PlannedNpcIntroduction] = Field(default_factory=list)
    object_names: list[str] = Field(default_factory=list)

    resolution: str = "conversation"
    observable_consequences: list[str] = Field(default_factory=list)
    protected_player_decisions: list[str] = Field(default_factory=list)
    pending_player_choice: str | None = None
    allow_new_complication: bool = False
    complication_source: str | None = None
    action_sequence: dict | None = None

    @property
    def allowed_new_npc_names(self) -> list[str]:
        return [item.canonical_name for item in self.allowed_new_npcs]

    def validator_payload(self) -> dict:
        """Compact payload deliberately omitting IDs that do not help the model judge prose."""
        return {
            "player_character": self.player_character_name,
            "acting_character": self.acting_character_name,
            "player_input": self.player_input,
            "scene_disposition": self.scene_disposition,
            "transition_type": self.transition_type,
            "source_location": self.source_location_path,
            "target_location": self.target_location_path,
            "present_characters": self.present_character_names,
            "known_absent_characters": self.known_absent_character_names,
            "allowed_new_npcs": [
                {
                    "canonical_name": item.canonical_name,
                    "role": item.role,
                    "reason": item.reason,
                }
                for item in self.allowed_new_npcs
            ],
            "objects_here": self.object_names,
            "resolution": self.resolution,
            "observable_consequences": self.observable_consequences,
            "protected_player_decisions": self.protected_player_decisions,
            "pending_player_choice": self.pending_player_choice,
            "allow_new_complication": self.allow_new_complication,
            "complication_source": self.complication_source,
            "action_sequence": self.action_sequence,
        }


class CoordinatedTurnPlanMixin(BaseModel):
    """Reusable validation for the additional inter-agent planning fields."""

    model_config = ConfigDict(extra="ignore")

    scene_disposition: Literal[
        "stay",
        "location_transition",
        "time_transition",
        "focus_transition",
        "sequence",
    ]
    npc_introductions: list[PlannedNpcIntroduction] = Field(
        default_factory=list,
        max_length=4,
    )

    @model_validator(mode="after")
    def unique_new_npc_names(self):
        names = [" ".join(item.canonical_name.casefold().split()) for item in self.npc_introductions]
        if len(names) != len(set(names)):
            raise ValueError("npc_introductions must use unique canonical names")
        return self
