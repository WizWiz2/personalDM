from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.game_master import WorldMoveKind
from app.models.addressed_response import AddressedResponse
from app.models.item_state import ItemStateSnapshot
from app.models.session_zero import NARRATIVE_PERSON, NarrativePerson


class PlannedNpcIntroduction(BaseModel):
    """One previously unknown NPC that this turn is allowed to introduce."""

    model_config = ConfigDict(extra="ignore")

    canonical_name: str = Field(min_length=2, max_length=120)
    # Preserve a proposed existing name before temporary-role normalization.
    identity_reference: str | None = Field(default=None, max_length=120)
    role: str | None = Field(default=None, max_length=200)
    current_goal: str | None = Field(default=None, max_length=500)
    description: str | None = Field(default=None, max_length=800)
    appearance: str | None = Field(default=None, max_length=800)
    voice: str | None = Field(default=None, max_length=400)
    temporary_name: bool = False
    personal_name_evidence: str | None = Field(default=None, max_length=500)
    reason: str = Field(min_length=2, max_length=500)
    after_action_index: int | None = Field(default=None, ge=0, le=7)
    # Typed resident slot (a location ID) this person keeps; one holder per slot.
    resident_slot: str | None = Field(default=None, max_length=64)
    # Brought here by a step's result, not found at the place: never a resident keeper.
    arrives: bool = False
    # Offscreen: an errand's target or a named person spoken of; only an arrival brings them.
    sent_for: bool = False


class PlannedPlaceIntroduction(BaseModel):
    """A place the world reveals inside a known place; it becomes a travel reference."""

    canonical_name: str = Field(min_length=2, max_length=80)
    profile: str = Field(min_length=32, max_length=600)
    parent_location_id: UUID


class PlannedItemIntroduction(BaseModel):
    """A significant physical object, justified by an approved current-turn outcome."""

    model_config = ConfigDict(extra="ignore")

    canonical_name: str = Field(min_length=2, max_length=120)
    description: str | None = Field(default=None, max_length=800)
    evidence: str = Field(min_length=5, max_length=500)
    owned_by_player: bool = False
    placement_detail: str | None = Field(default=None, max_length=300)
    after_action_index: int | None = Field(default=None, ge=0, le=7)
    copy_of_item_id: UUID | None = None
    reserved_item_id: UUID | None = None  # allocated by compiler, never by a model


class PlannedWorldMove(BaseModel):
    """A sourced, nonphysical independent development of an existing story thread.

    Physical moves, NPC introductions and item custody remain owned by their
    respective structured executors; a story beat alone cannot mutate them.
    """

    model_config = ConfigDict(extra="ignore")

    kind: WorldMoveKind  # arrival: the thread is fulfilled by a typed npc_introduction
    source: str = Field(min_length=8, max_length=1000)
    outcome: str = Field(min_length=12, max_length=1000)
    # NPC autonomous action must be grounded in one persisted CharacterGoal.
    actor_id: UUID | None = None
    destination_location_id: UUID | None = None  # NPC movement: exactly one known graph hop
    # Explicit clock phase for a scheduled deadline. Non-deadline beats leave it null.
    due_phase: Literal["dawn", "morning", "day", "evening", "night"] | None = None


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
    narrative_person: NarrativePerson = "second_singular"

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
    scene_time: str | None = None

    present_character_names: list[str] = Field(default_factory=list)
    known_absent_character_names: list[str] = Field(default_factory=list)
    allowed_new_npcs: list[PlannedNpcIntroduction] = Field(default_factory=list)
    allowed_new_items: list[PlannedItemIntroduction] = Field(default_factory=list)
    allowed_new_places: list[PlannedPlaceIntroduction] = Field(default_factory=list)
    world_move: PlannedWorldMove | None = None
    allowed_existing_npc_arrivals: list[ExistingNpcArrival] = Field(default_factory=list)
    object_names: list[str] = Field(default_factory=list)
    item_states: list[ItemStateSnapshot] = Field(default_factory=list)
    # Empty authoritative inventory differs from an older/incomplete frame.
    item_states_loaded: bool = False
    # Registered objects not physically available to the player.
    unavailable_item_names: list[str] = Field(default_factory=list)

    resolution: str = "conversation"
    identity_reveal_requested: bool = False
    addressed_response: AddressedResponse | None = None
    # Typed grant, the single source of NPC response: this cast member owns the beat.
    beat_owner_id: UUID | None = None
    beat_owner_name: str | None = None
    dramatic_mode: str = "calm"
    observable_consequences: list[str] = Field(default_factory=list)
    character_beats: list[str] = Field(default_factory=list)
    established_state: list[str] = Field(default_factory=list)
    established_subjects: list[str] = Field(default_factory=list)
    narration_guidance: list[str] = Field(default_factory=list)
    protected_player_decisions: list[str] = Field(default_factory=list)
    pending_player_choice: str | None = None
    allow_new_complication: bool = False
    complication_source: str | None = None
    action_sequence: dict | None = None

    @staticmethod
    def _public_blocked_outcome(step: dict) -> str:
        """Only the public result is prose evidence; raw execution diagnostics stay in audit."""
        return str(step.get("public_blocking_reason") or "").strip() or (
            "Действие не удалось завершить; нужно уточнить следующий шаг."
        )

    @model_validator(mode="after")
    def executed_sequence_owns_outcomes(self):
        """Executed steps, never Planner prose, own the observable surface of a sequence."""
        sequence = self.action_sequence or {}
        steps = sequence.get("steps")
        if not isinstance(steps, list) or not steps:
            return self

        executed: list[str] = []
        blocked = False
        for step in steps:
            if not isinstance(step, dict):
                continue
            status = step.get("status")
            if status == "completed":
                outcome = " ".join(str(step.get("observable_outcome") or "").split())
                if outcome and outcome not in executed:
                    executed.append(outcome)
            elif status == "blocked":
                blocked = True
                message = self._public_blocked_outcome(step)
                if message not in executed:
                    executed.append(message)
                break

        self.observable_consequences = executed

        if blocked:
            self.character_beats = []
            self.allow_new_complication = False
            self.complication_source = None
            self.narration_guidance = [
                "Заблокированный и последующие пропущенные шаги не произошли; опиши только "
                "завершённые шаги и фактическое препятствие без технических статусов движка."
            ]
            return self

        if not executed and self.acting_character_id is None:
            self.character_beats = []
            self.allow_new_complication = False
            self.complication_source = None
            self.narration_guidance = [
                "Структурное действие завершено без подтверждённого observable outcome. Опиши только "
                "само действие в текущей физической локации; не добавляй новые находки, факты, "
                "перемещение или сведения о другом месте."
            ]
        return self

    @property
    def allowed_new_npc_names(self) -> list[str]:
        return [item.canonical_name for item in self.allowed_new_npcs]

    @property
    def allowed_existing_npc_arrival_names(self) -> list[str]:
        return [item.canonical_name for item in self.allowed_existing_npc_arrivals]

    def trace_summary(self) -> dict:
        """Failure-diagnosis view of the frozen authority, recorded once per run; no prompt text."""
        steps = (self.action_sequence or {}).get("steps")
        return {
            "resolution": self.resolution,
            "scene_disposition": self.scene_disposition,
            "present_characters": self.present_character_names,
            "acting_character": self.acting_character_name,
            "beat_owner": self.beat_owner_name,
            "action_steps": [
                {"type": step.get("action_type"), "status": step.get("status")}
                for step in (steps if isinstance(steps, list) else [])
                if isinstance(step, dict)
            ],
            "established_subjects": self.established_subjects,
            "observable_consequence_count": len(self.observable_consequences),
        }

    def executed_steps(self) -> list[dict]:
        """Public view of what the executor did: the only step results prose must not overwrite."""
        steps = (self.action_sequence or {}).get("steps")
        result = []
        for step in steps if isinstance(steps, list) else []:
            if not isinstance(step, dict) or step.get("status") not in {"completed", "blocked"}:
                continue
            outcome = (
                self._public_blocked_outcome(step)
                if step.get("status") == "blocked"
                else " ".join(str(step.get("observable_outcome") or "").split())
            )
            result.append(
                {
                    "action_type": step.get("action_type"),
                    "status": step.get("status"),
                    "outcome": outcome or None,
                    "item_property_changes": step.get("item_property_changes") or [],
                    "step_index": step.get("step_index"),
                    "source_location_id": step.get("source_location_id"),
                    "target_location_id": step.get("target_location_id"),
                }
            )
        return result

    def inventory_receipts(self) -> list[dict]:
        """Executed physical transitions, including their actual before/after states."""
        steps = (self.action_sequence or {}).get("steps") or []
        keys = (
            "item_id", "item_name", "item_operation", "item_previous_owner_id",
            "item_previous_location_id", "item_previous_placement_detail",
            "item_result_owner_id", "item_result_location_id", "item_result_placement_detail",
        )
        receipts = []
        for index, step in enumerate(steps):
            if not isinstance(step, dict) or step.get("status") != "completed":
                continue
            frame = {"step_index": step.get("step_index", index),
                     "source_location_id": step.get("source_location_id"),
                     "target_location_id": step.get("target_location_id")}
            if step.get("action_type") == "inventory":
                receipts.append({**frame, **{key: step.get(key) for key in keys}})
            receipts.extend({**frame, **created} for created in step.get("created_items", []))
        return receipts

    def validator_payload(self) -> dict:
        """Typed facts the four bans are judged against, and nothing else."""
        return {
            "player_character": self.player_character_name,
            "player_input": self.player_input,
            "present_characters": self.present_character_names,
            "allowed_new_npcs": [
                {"canonical_name": item.canonical_name, "role": item.role}
                for item in self.allowed_new_npcs
            ],
            "allowed_existing_npc_arrivals": self.allowed_existing_npc_arrival_names,
            "known_absent_characters": self.known_absent_character_names,
            "established_state": self.established_state,
            "scene_time": self.scene_time,
            "allowed_new_items": [item.canonical_name for item in self.allowed_new_items],
            "objects_here": self.object_names,
            "item_states": [state.model_dump(mode="json") for state in self.item_states],
            "inventory_receipts": self.inventory_receipts(),
            "unavailable_items": self.unavailable_item_names,
            "approved_world_move": (
                self.world_move.model_dump(mode="json") if self.world_move else None
            ),
            "executed_steps": self.executed_steps(),
            "scene_disposition": self.scene_disposition,
            "transition_type": self.transition_type,
            "source_location": self.source_location_path,
            "target_location": self.target_location_path,
        }

    def narrator_payload(self) -> dict:
        """Four-bans facts plus optional rendering context. Nothing here is an obligation."""
        payload = {
            **self.validator_payload(),
            "allowed_new_npcs": [
                {
                    key: value
                    for key, value in item.model_dump(mode="json").items()
                    if key in {"canonical_name", "role", "description", "appearance", "voice"}
                }
                for item in self.allowed_new_npcs
            ],
            "beat_owner": (
                {"id": str(self.beat_owner_id), "name": self.beat_owner_name}
                if self.beat_owner_id else None
            ),
            "name_revealed": (
                {
                    "character": self.addressed_response.speaker_name,
                    "new_name": self.addressed_response.revealed_name,
                }
                if self.addressed_response and self.addressed_response.revealed_name
                else None
            ),
            "objects_here": self.object_names,
            "new_objects": [item.model_dump(mode="json") for item in self.allowed_new_items],
            "new_places": [place.canonical_name for place in self.allowed_new_places],
            "approved_world_move": (
                self.world_move.model_dump(mode="json") if self.world_move else None
            ),
            "resolution": self.resolution,
            "observable_consequences": self.observable_consequences,
            "character_beats": self.character_beats,
            "narration_guidance": self.narration_guidance,
            "narrative_person": NARRATIVE_PERSON[self.narrative_person],
            "dramatic_mode": self.dramatic_mode,
            "pending_player_choice": self.pending_player_choice,
            "allow_new_complication": self.allow_new_complication,
            "complication_source": self.complication_source,
        }
        return {key: value for key, value in payload.items() if value not in (None, "", [], {})}


__all__ = ["ExistingNpcArrival", "PlannedItemIntroduction", "PlannedNpcIntroduction", "PlannedWorldMove", "TurnAuthority"]
