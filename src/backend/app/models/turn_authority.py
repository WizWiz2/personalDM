from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.addressed_response import AddressedResponse


class PlannedNpcIntroduction(BaseModel):
    """One previously unknown NPC that this turn is allowed to introduce."""

    model_config = ConfigDict(extra="ignore")

    canonical_name: str = Field(min_length=2, max_length=120)
    # Preserve a proposed existing name before temporary-role normalization.
    identity_reference: str | None = Field(default=None, max_length=120)
    role: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=800)
    appearance: str | None = Field(default=None, max_length=800)
    voice: str | None = Field(default=None, max_length=400)
    temporary_name: bool = False
    personal_name_evidence: str | None = Field(default=None, max_length=500)
    reason: str = Field(min_length=2, max_length=500)
    after_action_index: int | None = Field(default=None, ge=0, le=7)
    # Typed resident slot (a location ID) this person keeps; one holder per slot.
    resident_slot: str | None = Field(default=None, max_length=64)


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
    scene_time: str | None = None

    present_character_names: list[str] = Field(default_factory=list)
    known_absent_character_names: list[str] = Field(default_factory=list)
    allowed_new_npcs: list[PlannedNpcIntroduction] = Field(default_factory=list)
    allowed_existing_npc_arrivals: list[ExistingNpcArrival] = Field(default_factory=list)
    object_names: list[str] = Field(default_factory=list)

    resolution: str = "conversation"
    identity_reveal_requested: bool = False
    addressed_response: AddressedResponse | None = None
    # Typed grant, the single source of NPC response: this cast member owns the beat.
    beat_owner_id: UUID | None = None
    beat_owner_name: str | None = None
    dramatic_mode: str = "calm"
    observable_consequences: list[str] = Field(default_factory=list)
    character_beats: list[str] = Field(default_factory=list)
    canon_constraints: list[str] = Field(default_factory=list)
    established_state: list[str] = Field(default_factory=list)
    established_subjects: list[str] = Field(default_factory=list)
    narration_guidance: list[str] = Field(default_factory=list)
    ending_hook: str = ""
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
            self.canon_constraints = []
            self.ending_hook = ""
            self.allow_new_complication = False
            self.complication_source = None
            self.narration_guidance = [
                "Заблокированный и последующие пропущенные шаги не произошли; опиши только "
                "завершённые шаги и фактическое препятствие без технических статусов движка."
            ]
            return self

        if not executed and self.acting_character_id is None:
            self.character_beats = []
            self.canon_constraints = []
            self.ending_hook = ""
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
                }
            )
        return result

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
            "resolution": self.resolution,
            "observable_consequences": self.observable_consequences,
            "character_beats": self.character_beats,
            "canon_constraints": self.canon_constraints,
            "narration_guidance": self.narration_guidance,
            "ending_hook": self.ending_hook,
            "dramatic_mode": self.dramatic_mode,
            "pending_player_choice": self.pending_player_choice,
            "allow_new_complication": self.allow_new_complication,
        }
        return {key: value for key, value in payload.items() if value not in (None, "", [], {})}


__all__ = ["ExistingNpcArrival", "PlannedNpcIntroduction", "TurnAuthority"]
