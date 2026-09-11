from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


IntentActionType = Literal[
    "service",
    "movement",
    "rest",
    "wait",
    "interaction",
    "observation",
    "inventory",
    "other",
]


class PlayerActionIntent(BaseModel):
    """One atomic action the human actually committed to in the latest turn.

    This is player authority, not a world result.  It intentionally contains no success/failure
    decision and no executable scene transition.  Those belong to later phases.
    """

    model_config = ConfigDict(extra="forbid")

    action_type: IntentActionType
    intent: str = Field(min_length=2, max_length=500)

    # Movement authority.  destination_location is the human-selected endpoint, not a route path.
    # allow_route_discovery means the human explicitly selected a plausible destination that is not
    # required to exist in the current graph yet.  It does not mean the move succeeds.
    destination_location: str | None = Field(default=None, max_length=255)
    allow_route_discovery: bool = False

    # Inventory authority is identity based.  IDs must come from authoritative context.
    item_id: UUID | None = None
    inventory_operation: Literal["take", "drop", "give", "place"] | None = None
    inventory_target_id: UUID | None = None

    # Time authority.  These fields describe what the player committed to waiting/resting through;
    # the compiler later turns them into a time transition when the outcome permits it.
    elapsed_time: str | None = Field(default=None, max_length=255)
    time_after: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def validate_domain_fields(self):
        if self.action_type == "movement":
            if not self.destination_location:
                raise ValueError("movement intent requires destination_location")
        elif self.destination_location is not None or self.allow_route_discovery:
            raise ValueError("only movement intent may carry destination authority")

        inventory_values = (
            self.item_id,
            self.inventory_operation,
            self.inventory_target_id,
        )
        if self.action_type == "inventory":
            if self.item_id is None or self.inventory_operation is None:
                raise ValueError("inventory intent requires item_id and inventory_operation")
            if self.inventory_operation == "give" and self.inventory_target_id is None:
                raise ValueError("give intent requires inventory_target_id")
        elif any(value is not None for value in inventory_values):
            raise ValueError("only inventory intent may carry inventory fields")

        if self.action_type not in {"rest", "wait"} and (
            self.elapsed_time is not None or self.time_after is not None
        ):
            raise ValueError("only rest/wait intent may carry time fields")
        return self


class PlayerIntentContract(BaseModel):
    """Frozen semantic authority extracted from exactly one human turn."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=2, max_length=500)
    actions: list[PlayerActionIntent] = Field(default_factory=list, max_length=8)

    # Speech/response ownership is separate from executable world actions.
    addressed_response_requested: bool = False
    addressed_character_name: str | None = Field(default=None, max_length=120)
    identity_reveal_requested: bool = False

    # Player-agency boundaries that remain unresolved after this input.
    pending_player_choice: str | None = Field(default=None, max_length=1000)
    protected_player_decisions: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def validate_response_target(self):
        if not self.addressed_response_requested:
            self.addressed_character_name = None
        return self


class PlayerIntentReview(BaseModel):
    """Narrow review of input -> intent IR fidelity only.

    World feasibility, route topology, success/failure and NPC reactions are deliberately outside
    this schema so the reviewer cannot rewrite execution semantics.
    """

    model_config = ConfigDict(extra="forbid")

    verdict: Literal["pass", "repair_required"]
    issues: list[str] = Field(default_factory=list, max_length=8)
    summary: str = Field(default="", max_length=800)


class ActionOutcomeDecision(BaseModel):
    """External/current-world result for one frozen action index."""

    model_config = ConfigDict(extra="forbid")

    action_index: int = Field(ge=0, le=7)
    resolution: Literal["auto_success", "requires_choice", "blocked"]
    safe_mundane: bool = False
    observable_outcome: str | None = Field(default=None, max_length=1000)
    blocking_reason: str | None = Field(default=None, max_length=1000)
    destination_profile: str | None = Field(default=None, max_length=1200)

    @model_validator(mode="after")
    def validate_result(self):
        if self.resolution == "blocked":
            if not self.blocking_reason:
                raise ValueError("blocked outcome requires blocking_reason")
            self.safe_mundane = False
        elif self.blocking_reason:
            raise ValueError("non-blocked outcome cannot carry blocking_reason")
        if self.safe_mundane and self.resolution != "auto_success":
            raise ValueError("safe_mundane requires auto_success")
        return self


class OutcomeNpcIntroduction(BaseModel):
    """One newly materialized person authorized by the external outcome resolver."""

    model_config = ConfigDict(extra="forbid")

    canonical_name: str = Field(min_length=2, max_length=120)
    role: str = Field(min_length=2, max_length=200)
    description: str = Field(min_length=32, max_length=800)
    appearance: str = Field(min_length=32, max_length=800)
    voice: str | None = Field(default=None, max_length=400)
    temporary_name: bool = True
    personal_name_evidence: str | None = Field(default=None, max_length=500)
    reason: str = Field(min_length=2, max_length=500)

    @model_validator(mode="after")
    def stable_name_requires_evidence(self):
        if not self.temporary_name and not self.personal_name_evidence:
            raise ValueError("stable NPC identity requires personal_name_evidence")
        return self


class TurnOutcomeDecision(BaseModel):
    """World-facing semantics after player intent has been frozen."""

    model_config = ConfigDict(extra="forbid")

    action_outcomes: list[ActionOutcomeDecision] = Field(default_factory=list, max_length=8)
    npc_introductions: list[OutcomeNpcIntroduction] = Field(default_factory=list, max_length=4)

    # These fields constrain narration/external consequences only; they cannot add player actions.
    resolution: Literal[
        "success",
        "partial_success",
        "failure",
        "uncertain",
        "conversation",
        "observation",
        "transition",
        "sequence",
    ] = "success"
    observable_consequences: list[str] = Field(default_factory=list, max_length=4)
    character_beats: list[str] = Field(default_factory=list, max_length=6)
    canon_constraints: list[str] = Field(default_factory=list, max_length=8)
    narration_guidance: list[str] = Field(default_factory=list, max_length=6)
    ending_hook: str = Field(default="", max_length=500)
    dramatic_mode: Literal["calm", "routine", "tense", "dangerous"] = "calm"
    allow_new_complication: bool = False
    complication_source: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def validate_complication(self):
        if self.allow_new_complication and not self.complication_source:
            raise ValueError("new complication requires complication_source")
        if not self.allow_new_complication:
            self.complication_source = None
        return self


class DestinationProfilePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_index: int = Field(ge=0, le=7)
    profile: str = Field(min_length=80, max_length=1000)


class DestinationProfilePatchSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    patches: list[DestinationProfilePatch] = Field(default_factory=list, max_length=4)


__all__ = [
    "ActionOutcomeDecision",
    "DestinationProfilePatch",
    "DestinationProfilePatchSet",
    "OutcomeNpcIntroduction",
    "PlayerActionIntent",
    "PlayerIntentContract",
    "PlayerIntentReview",
    "TurnOutcomeDecision",
]
