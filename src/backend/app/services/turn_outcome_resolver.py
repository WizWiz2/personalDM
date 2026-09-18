from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.player_intent import (
    ActionOutcomeDecision,
    DestinationProfilePatch,
    DestinationProfilePatchSet,
    PlayerIntentContract,
    TurnOutcomeDecision,
)
from app.models.turn import ChatMessage
from app.providers.llm_provider import LLMProvider, LLMProviderError
from app.services.action_plan_compiler import MissingDestinationProfile
from app.services.planning_context import planning_context
from app.services.role_model_router import RoleModelRouter, RoleModelSelection
from app.services.starter_identity import present_character_names
from app.services.turn_planner import TurnPlanningError
from app.services.turn_authority_resolvers import NpcIntroductionResolver
from app.services.entity_identity import identity_key
from app.services.player_intent_contract import contains_cjk

_OUTCOME_PROMPT = """[FROZEN INTENT OUTCOME RESOLVER]
The human's voluntary contribution is already frozen in PLAYER INTENT CONTRACT. Resolve only the
current-world/external result of those exact actions. Return exactly TurnOutcomeDecisionDraft.

Hard ownership boundaries:
- action_outcomes is a REQUIRED JSON field. Never omit it. Return exactly one action_outcome for every
  frozen action index; the structured response schema enforces the expected list length.
- Never add, delete, reorder, merge, reinterpret or continue the player's actions. action_index must
  refer to one existing frozen action. Return exactly one action_outcome for every action index.
- Every action_outcome MUST contain action_index and resolution.
- You may decide success, a concrete blocker, external consequences, observable information and NPC
  behavior. There is no dice/check resolver; do not postpone an action to a future check.
- resolution for each action must be exactly auto_success, requires_choice, or blocked.
- blocked requires a concrete blocking_reason. Non-blocked actions should not carry blocking_reason.
- safe_mundane=true is valid only for auto_success.
- requires_choice is only for a choice the human genuinely has not supplied. It is not a substitute
  for uncertainty or risk.
- Do not decide route topology. The deterministic compiler owns current location, exits, known
  locations and compound hop order. If authoritative context explicitly establishes an obstacle you
  may return blocked; otherwise resolve the fictional outcome and let the compiler enforce topology.
- A safe ordinary action with no established obstacle may be auto_success + safe_mundane=true.
- A destination being outside the current scene/building, or not yet recorded in the location
  catalogue, is not a physical obstacle. Explicit ordinary travel can discover a new public place.
  Do not confuse the pre-turn scene snapshot with a prohibition on changing it.
- Evaluate each action at its own point in the sequence. An obstacle on a later hop cannot block
  an earlier unobstructed hop. The compiler will enforce route availability in sequence order.
- auto_success means the action happens now; requires_choice means a specific missing player choice,
  never a request to confirm an already selected destination or inventory recipient.
- observable_outcome describes the result of that one action, not an extra player action.
- reaction is optional manner of that same outcome: a look, a pause, a line. It is not another
  result. If the action happens, reaction cannot say it did not. When any action_outcome exists,
  leave character_beats empty; the reaction belongs on the action.

NPC authority:
- Characters already listed in the context are existing identities, not npc_introductions. Never
  reintroduce them or move them from another scene. Ordinary travel, inventory transfers and waiting
  normally have npc_introductions=[]. A new introduction needs a specific encounter/contact reason.
- Only physically present characters may act unless this turn's frozen actions genuinely encounter,
  contact or cause the appearance of a new person.
- A genuinely new responder/person must be typed in npc_introductions; Narrator may not invent one.
- Addressing a new local person can be requested through addressed_response_requested even when
  actions=[] (speech is not an executable action). If such a person responds, create their typed
  introduction. Never replace that person with a named character located in another scene.
- Role/title-only identity is temporary: use temporary_name=true, canonical_name equal to a grounded
  role/designation (not an invented personal name), personal_name_evidence=null, and provide concrete
  description/appearance. Stable personal identity requires explicit current campaign evidence.
- Asking an existing character's name never creates a duplicate NPC; the published self-identification
  is handled after narration.

Narrative fields constrain only external presentation. They cannot authorize another player action.
For a stationary acknowledgement or speech-only turn, actions may be empty but still supply a
concrete external response, character beat or observable consequence. Do not add a player action.
Do not manufacture a complication in a calm routine turn without an established source. If
allow_new_complication=true, complication_source must identify that established source.

For an explicitly new route-discovered destination, destination_profile may describe stable public
physical traits/ordinary purpose in 2-4 Russian sentences. It is enrichment only; never use it to
change the destination or route.
"""

_PROFILE_PROMPT = """[NEW DESTINATION PROFILE ENRICHMENT]
Return exactly DestinationProfilePatchSet. You receive frozen action indices and destinations that
the deterministic compiler already identified as explicit new route-discovered places. Supply only a
stable public physical profile for each listed index: 2-4 Russian sentences, at least 80 characters,
ordinary purpose/appearance only. Do not change routes, actions, outcomes, NPCs or destination names.
The patches field is required and must contain exactly one patch per requested action index.
"""

_TRAVEL_PROMPT = """[ОБЫЧНОЕ ПУТЕШЕСТВИЕ: ПРОВЕРКА ФИЗИЧЕСКИХ ПРЕПЯТСТВИЙ]
Действия игрока уже выбраны. Для каждого перехода проверь только наличие конкретного физического
препятствия, установленного контекстом мира или вводом игрока. Если препятствия нет, верни
blocking_reason=null и evidence_quote=null. Если есть — назови его и процитируй дословное основание
в evidence_quote. Не выдумывай запертые двери, охрану, запреты или опасности.

Правила движка: граф проверяется отдельно. Новые места разрешено открывать обычным путешествием;
их регистрация, профиль и новая сцена создаются ПОСЛЕ этой проверки. Отсутствие записи места в БД,
отсутствие его в текущей сцене, нахождение снаружи здания и ещё не исполненный переход НЕ являются
препятствиями. Не проверяй предварительную регистрацию и не требуй подтверждать выбранный адрес.
Не добавляй персонажей, диалог, эмоции героя или дополнительные действия.
Верни OrdinaryTravelObstacles, ровно по одному элементу на каждый action_index.
"""


class TravelObstacleDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action_index: int = Field(ge=0, le=7)
    blocking_reason: str | None
    evidence_quote: str | None


def _travel_wire_model(action_count: int, *, evidence: str = ""):
    class IndexedTravelObstacle(TravelObstacleDraft):
        action_index: Literal[tuple(range(action_count))]

    class OrdinaryTravelObstacles(BaseModel):
        model_config = ConfigDict(extra="forbid")
        obstacles: list[IndexedTravelObstacle] = Field(min_length=action_count, max_length=action_count)

        @model_validator(mode="after")
        def validate_grounding(self):
            indices = [item.action_index for item in self.obstacles]
            if sorted(indices) != list(range(action_count)):
                raise ValueError(f"travel obstacles must cover exactly indices {list(range(action_count))}")
            for item in self.obstacles:
                if (item.blocking_reason or "").strip():
                    quote = (item.evidence_quote or "").strip()
                    if not quote or quote not in evidence:
                        raise ValueError("travel blocker lacks a verbatim world/input evidence quote")
            return self

    return OrdinaryTravelObstacles

_ACTION_RESOLUTIONS = {"auto_success", "requires_choice", "blocked"}
_TURN_RESOLUTIONS = {
    "success",
    "partial_success",
    "failure",
    "uncertain",
    "conversation",
    "observation",
    "transition",
    "sequence",
}
_DRAMATIC_MODES = {"calm", "routine", "tense", "dangerous"}


class ActionOutcomeDraft(BaseModel):
    """LLM-facing outcome shape without cross-field semantic validators."""

    model_config = ConfigDict(extra="ignore")

    action_index: int = Field(ge=0, le=7)
    resolution: str = Field(min_length=2, max_length=32)
    safe_mundane: bool = False
    observable_outcome: str | None = None
    reaction: str | None = None
    blocking_reason: str | None = None
    destination_profile: str | None = None


class OutcomeNpcIntroductionDraft(BaseModel):
    model_config = ConfigDict(extra="ignore")

    canonical_name: str = Field(min_length=2, max_length=120)
    role: str = Field(min_length=2, max_length=120)
    description: str = Field(min_length=32, max_length=800)
    appearance: str = Field(min_length=32, max_length=800)
    voice: str | None = None
    temporary_name: bool = True
    personal_name_evidence: str | None = None
    reason: str = Field(min_length=2, max_length=500)

    @model_validator(mode="after")
    def validate_identity(self):
        # Run this inside the provider's bounded schema-repair loop, before compilation.
        if contains_cjk(self.role) or identity_key(self.role) in {
            identity_key(value) for value in NpcIntroductionResolver.SYNTHETIC_PLACEHOLDERS
        }:
            raise ValueError("NPC identity needs a short readable grounded role")
        NpcIntroductionResolver.sanitize_introductions([self])
        return self


class TurnOutcomeDecisionDraft(BaseModel):
    """Permissive semantic draft with a mandatory action coverage field.

    Cross-field meaning is normalized later, but ``action_outcomes`` has no default on purpose.
    Otherwise Ollama's native schema considers ``{}`` valid and an actionful turn can silently become
    an empty decision before deterministic coverage checks ever get useful evidence.
    """

    model_config = ConfigDict(extra="ignore")

    action_outcomes: list[ActionOutcomeDraft] = Field(max_length=8)
    npc_introductions: list[OutcomeNpcIntroductionDraft] = Field(default_factory=list, max_length=4)
    resolution: str = "success"
    observable_consequences: list[str] = Field(default_factory=list, max_length=4)
    character_beats: list[str] = Field(default_factory=list, max_length=6)
    canon_constraints: list[str] = Field(default_factory=list, max_length=8)
    narration_guidance: list[str] = Field(default_factory=list, max_length=6)
    ending_hook: str = ""
    dramatic_mode: str = "calm"
    allow_new_complication: bool = False
    complication_source: str | None = None


def _outcome_wire_model(
    action_count: int, *, allow_choice: bool = True
) -> type[TurnOutcomeDecisionDraft]:
    """Constrain only structural coverage at the model boundary.

    The model remains free to describe each external result permissively. The list cardinality is not
    semantic inference, though: it is already known exactly from frozen player authority, so native
    structured decoding should enforce it instead of allowing a vacuous list through to later guards.
    """

    class SuccessfulActionOutcomeDraft(ActionOutcomeDraft):
        resolution: Literal["auto_success"]
        observable_outcome: str = Field(min_length=2, max_length=1000)

    class BlockedActionOutcomeDraft(ActionOutcomeDraft):
        resolution: Literal["blocked"]
        blocking_reason: str = Field(min_length=2, max_length=1000)

    action_model = (
        ActionOutcomeDraft
        if allow_choice
        else SuccessfulActionOutcomeDraft | BlockedActionOutcomeDraft
    )

    class ExactTurnOutcomeDecisionDraft(TurnOutcomeDecisionDraft):
        action_outcomes: list[action_model] = Field(
            min_length=action_count,
            max_length=action_count,
        )
        npc_introductions: list[OutcomeNpcIntroductionDraft] = Field(max_length=4)

        @model_validator(mode="after")
        def validate_action_indices(self):
            if sorted(item.action_index for item in self.action_outcomes) != list(range(action_count)):
                raise ValueError(f"action_outcomes must cover exactly indices {list(range(action_count))}")
            return self

    if action_count == 0:
        # No executable action is valid for dialogue/acknowledgement. It still needs an external
        # response or observable beat, otherwise the downstream authority receives a vacuous plan.
        class ResponsiveTurnOutcomeDecisionDraft(ExactTurnOutcomeDecisionDraft):
            observable_consequences: list[str] = Field(min_length=1, max_length=4)

        ResponsiveTurnOutcomeDecisionDraft.__name__ = "TurnOutcomeDecisionDraft"
        return ResponsiveTurnOutcomeDecisionDraft

    ExactTurnOutcomeDecisionDraft.__name__ = "TurnOutcomeDecisionDraft"
    return ExactTurnOutcomeDecisionDraft


def _profile_wire_model(
    patch_count: int, *, action_indices: list[int] | None = None,
) -> type[DestinationProfilePatchSet]:
    expected = sorted(action_indices if action_indices is not None else range(patch_count))
    class IndexedDestinationProfilePatch(DestinationProfilePatch):
        action_index: Literal[tuple(expected)]

    class ExactDestinationProfilePatchSet(DestinationProfilePatchSet):
        patches: list[IndexedDestinationProfilePatch] = Field(
            min_length=patch_count,
            max_length=patch_count,
        )

        @model_validator(mode="after")
        def validate_action_indices(self):
            if sorted(item.action_index for item in self.patches) != expected:
                raise ValueError(f"destination profiles must cover exactly indices {expected}")
            return self

    ExactDestinationProfilePatchSet.__name__ = "DestinationProfilePatchSet"
    return ExactDestinationProfilePatchSet


def _compact(value: object) -> str:
    return " ".join(str(value or "").split())


def _bounded_strings(values: list[str], limit: int) -> list[str]:
    result: list[str] = []
    for raw in values[:limit]:
        value = _compact(raw)
        if value:
            result.append(value)
    return result


def normalize_outcome_draft(
    draft: TurnOutcomeDecisionDraft,
    contract: PlayerIntentContract,
) -> TurnOutcomeDecision:
    """Turn permissive model output into strict world-facing semantics deterministically."""

    expected = set(range(len(contract.actions)))
    got = [item.action_index for item in draft.action_outcomes]
    if len(got) != len(set(got)) or set(got) != expected:
        raise TurnPlanningError(
            "outcome resolver did not preserve frozen action coverage; "
            f"expected={sorted(expected)} got={sorted(got)}"
        )

    action_outcomes: list[dict[str, Any]] = []
    for item in draft.action_outcomes:
        resolution = _compact(item.resolution).casefold()
        if resolution not in _ACTION_RESOLUTIONS:
            raise TurnPlanningError(
                f"outcome action {item.action_index} returned unknown resolution={resolution!r}"
            )
        blocking_reason = _compact(item.blocking_reason) or None
        if resolution == "blocked" and not blocking_reason:
            raise TurnPlanningError(
                f"blocked outcome for action {item.action_index} has no concrete blocking reason"
            )
        action_outcomes.append(
            {
                "action_index": item.action_index,
                "resolution": resolution,
                "safe_mundane": bool(item.safe_mundane) if resolution == "auto_success" else False,
                "observable_outcome": _compact(item.observable_outcome) or None,
                "reaction": _compact(item.reaction) or None,
                "blocking_reason": blocking_reason if resolution == "blocked" else None,
                "destination_profile": _compact(item.destination_profile) or None,
            }
        )

    introductions: list[dict[str, Any]] = []
    for index, npc in enumerate(draft.npc_introductions):
        canonical_name = _compact(npc.canonical_name)
        role = _compact(npc.role)
        description = _compact(npc.description)
        appearance = _compact(npc.appearance)
        reason = _compact(npc.reason)
        if not canonical_name or not role:
            raise TurnPlanningError(f"NPC introduction {index} is missing designation or role")
        if len(description) < 32 or len(appearance) < 32:
            raise TurnPlanningError(
                f"NPC introduction {index} lacks a concrete description/appearance"
            )
        if not reason:
            raise TurnPlanningError(f"NPC introduction {index} is missing appearance reason")
        evidence = _compact(npc.personal_name_evidence) or None
        # Missing evidence cannot promote a personal identity. Downgrading to a temporary role is
        # conservative and preserves the person without inventing stable canon.
        temporary_name = bool(npc.temporary_name) or not evidence
        introductions.append(
            {
                "canonical_name": canonical_name,
                "role": role,
                "description": description,
                "appearance": appearance,
                "voice": _compact(npc.voice) or None,
                "temporary_name": temporary_name,
                "personal_name_evidence": evidence if not temporary_name else None,
                "reason": reason,
            }
        )

    resolution = _compact(draft.resolution).casefold()
    if resolution not in _TURN_RESOLUTIONS:
        resolution = "success"
    dramatic_mode = _compact(draft.dramatic_mode).casefold()
    if dramatic_mode not in _DRAMATIC_MODES:
        dramatic_mode = "calm"
    complication_source = _compact(draft.complication_source) or None
    allow_complication = bool(draft.allow_new_complication and complication_source)

    public_outcomes = [
        {key: value for key, value in item.items() if key != "reaction"}
        for item in action_outcomes
    ]
    return TurnOutcomeDecision.model_validate(
        {
            "action_outcomes": public_outcomes,
            "npc_introductions": introductions,
            "resolution": resolution,
            "observable_consequences": _bounded_strings(
                list(
                    dict.fromkeys(
                        [
                            *draft.observable_consequences,
                            *[
                                item["observable_outcome"]
                                for item in action_outcomes
                                if item.get("observable_outcome")
                            ],
                        ]
                    )
                ),
                4,
            ),
            "character_beats": _bounded_strings(
                [
                    item["reaction"]
                    for item in action_outcomes
                    if item.get("reaction")
                ]
                if any(item.get("observable_outcome") for item in action_outcomes)
                else draft.character_beats,
                6,
            ),
            "canon_constraints": _bounded_strings(draft.canon_constraints, 8),
            "narration_guidance": _bounded_strings(draft.narration_guidance, 6),
            "ending_hook": _compact(draft.ending_hook),
            "dramatic_mode": dramatic_mode,
            "allow_new_complication": allow_complication,
            "complication_source": complication_source if allow_complication else None,
        }
    )


class TurnOutcomeResolver:
    """Resolve external consequences after player authority is frozen."""

    def __init__(self, router: RoleModelRouter):
        self._router = router
        self._provider = LLMProvider()
        self.audit: list[dict] = []

    @staticmethod
    def _context(context_messages: list[ChatMessage]) -> str:
        # Outcome resolution needs campaign state/facts, but not the old prose transcript. The first
        # compiled system message is the authoritative layered context used by Planner today.
        return planning_context(context_messages)

    async def _resolve_ordinary_travel(self, selection, context_messages, player_input, contract):
        """A travel-only decision cannot invent NPCs or confuse discovery with missing authority."""
        context = self._context(context_messages)
        wire = _travel_wire_model(len(contract.actions), evidence=context + "\n" + player_input)
        data = await self._router.generate_json(
            self._provider, selection,
            [
                ChatMessage(role="system", content=_TRAVEL_PROMPT + "\n[OUTPUT JSON SCHEMA]\n"
                            + json.dumps(wire.model_json_schema(), ensure_ascii=False)),
                ChatMessage(role="user", content=context + "\n[ВВОД ИГРОКА]\n" + player_input
                            + "\n[ВЫБРАННЫЕ ДЕЙСТВИЯ]\n" + contract.model_dump_json()),
            ],
            max_tokens=700, temperature=0, response_model=wire,
        )
        draft = wire.model_validate(data)
        outcomes = []
        evidence = context + "\n" + player_input
        for obstacle in draft.obstacles:
            reason = (obstacle.blocking_reason or "").strip()
            quote = (obstacle.evidence_quote or "").strip()
            if reason and (not quote or quote not in evidence):
                raise TurnPlanningError("travel blocker lacks a verbatim world/input evidence quote")
            if obstacle.action_index >= len(contract.actions):
                raise TurnPlanningError("travel obstacle refers to an unknown action")
            action = contract.actions[obstacle.action_index]
            outcomes.append(ActionOutcomeDecision(
                action_index=obstacle.action_index,
                resolution="blocked" if reason else "auto_success",
                safe_mundane=not bool(reason),
                blocking_reason=reason or None,
                observable_outcome=None if reason else f"Переход в место «{action.destination_location}» завершён.",
            ))
        decision = TurnOutcomeDecision(action_outcomes=outcomes, resolution="sequence")
        self._validate_coverage(contract, decision)
        self.audit.append({"phase": "ordinary_travel", "draft": draft.model_dump(mode="json"),
                           "decision": decision.model_dump(mode="json")})
        return decision

    @staticmethod
    def _validate_coverage(
        contract: PlayerIntentContract,
        decision: TurnOutcomeDecision,
    ) -> None:
        expected = set(range(len(contract.actions)))
        got = [item.action_index for item in decision.action_outcomes]
        if len(got) != len(set(got)) or set(got) != expected:
            raise TurnPlanningError(
                "outcome resolver did not preserve frozen action coverage; "
                f"expected={sorted(expected)} got={sorted(got)}"
            )

    @staticmethod
    def _normalize_temporary_identities(decision: TurnOutcomeDecision) -> TurnOutcomeDecision:
        """A temporary role cannot smuggle an unsupported personal label into entity identity."""
        normalized = decision.model_copy(deep=True)
        for introduction in normalized.npc_introductions:
            introduction.identity_reference = (
                introduction.identity_reference or introduction.canonical_name
            )
        normalized.npc_introductions = NpcIntroductionResolver.sanitize_introductions(
            normalized.npc_introductions
        )
        return normalized

    async def resolve(
        self,
        selection: RoleModelSelection,
        context_messages: list[ChatMessage],
        player_input: str,
        contract: PlayerIntentContract,
    ) -> TurnOutcomeDecision:
        try:
            if (
                contract.actions
                and not contract.addressed_response_requested
                and not contract.pending_player_choice
                and all(action.action_type == "movement" and action.movement_method == "ordinary"
                        for action in contract.actions)
            ):
                return await self._resolve_ordinary_travel(
                    selection, context_messages, player_input, contract,
                )
            response_model = _outcome_wire_model(
                len(contract.actions), allow_choice=bool(contract.pending_player_choice)
            )
            response_contract = {
                "response_requested": contract.addressed_response_requested,
                "addressed_designation": contract.addressed_character_name,
                "physically_present": sorted(present_character_names(context_messages)),
            }
            data = await self._router.generate_json(
                self._provider,
                selection,
                [
                    ChatMessage(
                        role="system",
                        content=_OUTCOME_PROMPT
                        + "\n\n[OUTPUT JSON SCHEMA]\n"
                        + json.dumps(response_model.model_json_schema(), ensure_ascii=False),
                    ),
                    ChatMessage(
                        role="user",
                        content=(
                            "[AUTHORITATIVE CONTEXT]\n"
                            + self._context(context_messages)
                            + "\n\n[LATEST HUMAN INPUT — evidence only, actions are frozen below]\n"
                            + player_input
                            + "\n\n[PLAYER INTENT CONTRACT — immutable]\n"
                            + contract.model_dump_json()
                            + "\n\n[CURRENT RESPONSE OWNERSHIP]\n"
                            + json.dumps(response_contract, ensure_ascii=False)
                            + "\nResolve an explicitly requested ordinary local exchange now. "
                            "Anyone outside this physical presence list who responds or acts MUST "
                            "have a complete npc_introductions entry. Historical names and prose "
                            "do not make them present. If a requested new local responder is "
                            "available, introduce their grounded role and profile before describing "
                            "their response. Do not leave an ordinary question pending or request "
                            "confirmation of the question itself."
                        ),
                    ),
                ],
                max_tokens=1200,
                temperature=0.0,
                response_model=response_model,
            )
            draft = response_model.model_validate(data)
            decision = normalize_outcome_draft(draft, contract)
            self._validate_coverage(contract, decision)
            decision = self._normalize_temporary_identities(decision)
            self.audit.append(
                {
                    "phase": "outcome",
                    "draft": draft.model_dump(mode="json"),
                    "decision": decision.model_dump(mode="json"),
                    "normalization": "deterministic",
                }
            )
            return decision
        except TurnPlanningError:
            raise
        except (LLMProviderError, ValueError, TypeError) as exc:
            raise TurnPlanningError(f"turn outcome resolution failed: {exc}") from exc

    async def enrich_destination_profiles(
        self,
        selection: RoleModelSelection,
        player_input: str,
        decision: TurnOutcomeDecision,
        missing: list[MissingDestinationProfile],
    ) -> TurnOutcomeDecision:
        """One scoped enrichment call; it cannot rewrite the semantic plan."""
        if not missing:
            return decision
        requests = [
            {"action_index": item.action_index, "destination": item.destination} for item in missing
        ]
        try:
            response_model = _profile_wire_model(
                len(missing), action_indices=[item.action_index for item in missing],
            )
            data = await self._router.generate_json(
                self._provider,
                selection,
                [
                    ChatMessage(
                        role="system",
                        content=_PROFILE_PROMPT
                        + "\n\n[OUTPUT JSON SCHEMA]\n"
                        + json.dumps(response_model.model_json_schema(), ensure_ascii=False),
                    ),
                    ChatMessage(
                        role="user",
                        content=(
                            "LATEST HUMAN INPUT:\n"
                            + player_input
                            + "\n\nPROFILE REQUESTS:\n"
                            + json.dumps(requests, ensure_ascii=False)
                        ),
                    ),
                ],
                max_tokens=700,
                temperature=0.0,
                response_model=response_model,
            )
            patches = response_model.model_validate(data)
        except (LLMProviderError, ValueError, TypeError) as exc:
            raise TurnPlanningError(f"destination profile enrichment failed: {exc}") from exc

        expected = {item.action_index for item in missing}
        by_index = {item.action_index: item.profile.strip() for item in patches.patches}
        if set(by_index) != expected:
            raise TurnPlanningError(
                "destination profile enrichment did not cover exactly the requested actions"
            )
        enriched = decision.model_copy(deep=True)
        outcomes = {item.action_index: item for item in enriched.action_outcomes}
        for index, profile in by_index.items():
            outcomes[index].destination_profile = profile
        self.audit.append(
            {"phase": "destination_profiles", "requests": requests, "patches": by_index}
        )
        return enriched


__all__ = [
    "ActionOutcomeDraft",
    "OutcomeNpcIntroductionDraft",
    "TurnOutcomeDecisionDraft",
    "TurnOutcomeResolver",
    "normalize_outcome_draft",
]
