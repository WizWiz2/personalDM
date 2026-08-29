from __future__ import annotations

import re
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

    @staticmethod
    def _player_facing_blocking_reason(value: object) -> str | None:
        """Translate known control-plane blockers without publishing engine vocabulary."""
        reason = " ".join(str(value or "").split()).strip()
        if not reason:
            return None
        folded = reason.casefold()

        mappings = (
            (
                ("player destination is unresolved", "existing route is required"),
                "Из текущего места пока не виден подтверждённый путь туда.",
            ),
            (
                ("player destination is not authorized",),
                "Неясно, куда именно ведёт этот шаг; путь остаётся прежним.",
            ),
            (
                ("not an available exit", "destination is not an available exit"),
                "Из текущего места туда нет доступного прохода.",
            ),
            (
                ("destination route is currently inactive", "route is currently inactive"),
                "Путь туда сейчас недоступен.",
            ),
            (
                ("destination exit has not been discovered", "exit has not been discovered"),
                "Путь туда пока не обнаружен.",
            ),
            (
                (
                    "resolved to the current physical location",
                    "use stay/focus_transition",
                    "claiming physical travel",
                ),
                "Ты остаёшься там, где уже стоишь.",
            ),
            (
                ("matches multiple existing routes",),
                "Из текущего места туда ведёт больше одного пути; нужно уточнить направление.",
            ),
            (
                ("destination location is empty",),
                "Неясно, куда именно ведёт этот шаг; путь остаётся прежним.",
            ),
            (
                ("requires player input",),
                "Нужно уточнить, что именно ты пытаешься сделать.",
            ),
        )
        for tokens, text in mappings:
            if any(token in folded for token in tokens):
                return text

        technical = (
            "requires a check",
            "route discovery",
            "source_scene",
            "target_scene",
            "source_location_id",
            "target_location_id",
            "location_transition",
            "focus_transition",
            "scene_disposition",
            "action_sequence",
            "planner",
            "validator",
            "control-plane",
        )
        if any(token in folded for token in technical):
            return None
        if re.search(
            r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
            r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
            reason,
            flags=re.IGNORECASE,
        ):
            return None
        return reason

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
                reason = self._player_facing_blocking_reason(step.get("blocking_reason"))
                message = reason or "Путь вперёд остаётся закрыт."
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

    def validator_payload(self) -> dict:
        """Compact authority for continuity judging, without competing prompt prose."""
        payload = {
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
        if self.acting_character_id and self.acting_character_name:
            from app.services.mixed_actor_response_guard import actor_response_contract

            contract = actor_response_contract(self)
            if contract:
                payload["actor_turn_contract"] = contract
        return payload

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
                "player_agency_contract": {
                    "response_focus": "world_or_npc_response_to_current_input",
                    "do_not_restate_player_voluntary_action": True,
                    "do_not_extend_player_voluntary_action": True,
                    "do_not_assign_player_thoughts_emotions_or_decisions": True,
                    "do_not_invent_player_dialogue": True,
                    "perspective": "second_person_only_for_immediate_perception_or_external_effect",
                    "stop_before_next_player_choice": True,
                },
                "execution_section": "[EXECUTED ACTION SEQUENCE]",
            }
        )
        return payload


__all__ = ["ExistingNpcArrival", "PlannedNpcIntroduction", "TurnAuthority"]
