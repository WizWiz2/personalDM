from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.player_intent import (
    DestinationProfilePatchSet,
    PlayerIntentContract,
    TurnOutcomeDecision,
)
from app.models.turn import ChatMessage
from app.providers.llm_provider import LLMProvider, LLMProviderError
from app.services.action_plan_compiler import MissingDestinationProfile
from app.services.role_model_router import RoleModelRouter, RoleModelSelection
from app.services.turn_planner import TurnPlanningError


_OUTCOME_PROMPT = """[FROZEN INTENT OUTCOME RESOLVER]
The human's voluntary contribution is already frozen in PLAYER INTENT CONTRACT. Resolve only the
current-world/external result of those exact actions. Return exactly TurnOutcomeDecisionDraft.

Hard ownership boundaries:
- Never add, delete, reorder, merge, reinterpret or continue the player's actions. action_index must
  refer to one existing frozen action. Return exactly one action_outcome for every action index.
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
- observable_outcome describes the result of that one action, not an extra player action.

NPC authority:
- Only physically present characters may act unless this turn's frozen actions genuinely encounter,
  contact or cause the appearance of a new person.
- A genuinely new responder/person must be typed in npc_introductions; Narrator may not invent one.
- Role/title-only identity is temporary: use temporary_name=true, canonical_name equal to a grounded
  role/designation (not an invented personal name), personal_name_evidence=null, and provide concrete
  description/appearance. Stable personal identity requires explicit current campaign evidence.
- Asking an existing character's name never creates a duplicate NPC; the published self-identification
  is handled after narration.

Narrative fields constrain only external presentation. They cannot authorize another player action.
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
Return one patch per requested index and no other indices.
"""

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
    resolution: str = ""
    safe_mundane: bool = False
    observable_outcome: str | None = None
    blocking_reason: str | None = None
    destination_profile: str | None = None


class OutcomeNpcIntroductionDraft(BaseModel):
    model_config = ConfigDict(extra="ignore")

    canonical_name: str = ""
    role: str = ""
    description: str = ""
    appearance: str = ""
    voice: str | None = None
    temporary_name: bool = True
    personal_name_evidence: str | None = None
    reason: str = ""


class TurnOutcomeDecisionDraft(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action_outcomes: list[ActionOutcomeDraft] = Field(default_factory=list, max_length=8)
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

    return TurnOutcomeDecision.model_validate(
        {
            "action_outcomes": action_outcomes,
            "npc_introductions": introductions,
            "resolution": resolution,
            "observable_consequences": _bounded_strings(draft.observable_consequences, 4),
            "character_beats": _bounded_strings(draft.character_beats, 6),
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
        if not context_messages:
            return ""
        # Outcome resolution needs campaign state/facts, but not the old prose transcript. The first
        # compiled system message is the authoritative layered context used by Planner today.
        return context_messages[0].content

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
        used: set[str] = set()
        for introduction in normalized.npc_introductions:
            if introduction.temporary_name and not introduction.personal_name_evidence:
                base = " ".join(introduction.role.split())
                candidate = base[0].upper() + base[1:] if base else introduction.canonical_name
                if candidate.casefold() in used:
                    suffix = 2
                    while f"{candidate} {suffix}".casefold() in used:
                        suffix += 1
                    candidate = f"{candidate} {suffix}"
                introduction.canonical_name = candidate
            used.add(introduction.canonical_name.casefold())
        return normalized

    async def resolve(
        self,
        selection: RoleModelSelection,
        context_messages: list[ChatMessage],
        player_input: str,
        contract: PlayerIntentContract,
    ) -> TurnOutcomeDecision:
        try:
            data = await self._router.generate_json(
                self._provider,
                selection,
                [
                    ChatMessage(role="system", content=_OUTCOME_PROMPT),
                    ChatMessage(
                        role="user",
                        content=(
                            "[AUTHORITATIVE CONTEXT]\n"
                            + self._context(context_messages)
                            + "\n\n[LATEST HUMAN INPUT — evidence only, actions are frozen below]\n"
                            + player_input
                            + "\n\n[PLAYER INTENT CONTRACT — immutable]\n"
                            + contract.model_dump_json()
                        ),
                    ),
                ],
                max_tokens=1200,
                temperature=0.0,
                response_model=TurnOutcomeDecisionDraft,
            )
            draft = TurnOutcomeDecisionDraft.model_validate(data)
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
            {"action_index": item.action_index, "destination": item.destination}
            for item in missing
        ]
        try:
            data = await self._router.generate_json(
                self._provider,
                selection,
                [
                    ChatMessage(role="system", content=_PROFILE_PROMPT),
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
                response_model=DestinationProfilePatchSet,
            )
            patches = DestinationProfilePatchSet.model_validate(data)
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
