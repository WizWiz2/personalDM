from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


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


class ExistingNpcArrival(BaseModel):
    """A known character that may become present without being recreated as a new entity."""

    model_config = ConfigDict(extra="ignore")

    entity_id: UUID
    canonical_name: str = Field(min_length=2, max_length=120)
    reason: str = Field(min_length=2, max_length=500)


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
    allowed_existing_npc_arrivals: list[ExistingNpcArrival] = Field(default_factory=list)
    object_names: list[str] = Field(default_factory=list)

    resolution: str = "conversation"
    dramatic_mode: str = "calm"
    observable_consequences: list[str] = Field(default_factory=list)
    character_beats: list[str] = Field(default_factory=list)
    canon_constraints: list[str] = Field(default_factory=list)
    narration_guidance: list[str] = Field(default_factory=list)
    ending_hook: str = ""
    protected_player_decisions: list[str] = Field(default_factory=list)
    pending_player_choice: str | None = None
    allow_new_complication: bool = False
    complication_source: str | None = None
    action_sequence: dict | None = None

    @property
    def allowed_new_npc_names(self) -> list[str]:
        return [item.canonical_name for item in self.allowed_new_npcs]

    @property
    def allowed_existing_npc_arrival_names(self) -> list[str]:
        return [item.canonical_name for item in self.allowed_existing_npc_arrivals]

    def validator_payload(self) -> dict:
        """Compact authority for continuity judging, without competing prompt prose."""
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
            "allowed_existing_npc_arrivals": [
                {
                    "entity_id": str(item.entity_id),
                    "canonical_name": item.canonical_name,
                    "reason": item.reason,
                }
                for item in self.allowed_existing_npc_arrivals
            ],
            "objects_here": self.object_names,
            "resolution": self.resolution,
            "dramatic_mode": self.dramatic_mode,
            "observable_consequences": self.observable_consequences,
            "canon_constraints": self.canon_constraints,
            "protected_player_decisions": self.protected_player_decisions,
            "pending_player_choice": self.pending_player_choice,
            "allow_new_complication": self.allow_new_complication,
            "complication_source": self.complication_source,
            "action_sequence": self.action_sequence,
        }

    def narrator_payload(self) -> dict:
        """Complete prose rendering contract derived from the same authority object."""
        payload = self.validator_payload()
        payload.update(
            {
                "allowed_new_npcs": [
                    item.model_dump(mode="json") for item in self.allowed_new_npcs
                ],
                "character_beats": self.character_beats,
                "narration_guidance": self.narration_guidance,
                "ending_hook": self.ending_hook,
            }
        )
        if self.action_sequence:
            # Keep a human/debugger-visible marker while the data itself remains part of
            # this single authority object, not a second injected contract.
            payload["execution_section"] = "[EXECUTED ACTION SEQUENCE]"
        return payload


__all__ = ["ExistingNpcArrival", "PlannedNpcIntroduction", "TurnAuthority"]
