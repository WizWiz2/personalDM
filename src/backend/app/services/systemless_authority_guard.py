from __future__ import annotations

import re

from app.services.narration_repetition_guard import (
    NarrationRepetitionGuard,
    RepetitionMatch,
)
from app.services.scene_transition_executor import SceneTransitionExecutor
from app.services.turn_authority_planner import CoordinatedTurnPlan, TurnAuthorityPlanner
from app.services.turn_planner import ActionSequencePlan, SceneTransitionPlan

_INSTALLED = False

_DIALOGUE_INPUT_RE = re.compile(
    r"(?:\?|^\s*(?:кто|что|где|когда|почему|зачем|как|"
    r"расскаж\w*|скаж\w*|объясн\w*|помн\w*|можете|"
    r"who|what|where|when|why|how|tell\b|explain\b|do\s+you\b))",
    flags=re.IGNORECASE,
)
_DIRECT_CONTACT_RU_RE = re.compile(
    rf"\b(?:расспраш\w*|спрашива\w*)\s+"
    rf"(?:[^.!?]{{0,24}}\s)?{TurnAuthorityPlanner.GENERIC_CONTACT_ROLE_RU}\b",
    flags=re.IGNORECASE,
)

_SYSTEMLESS_PROMPT = """

[SYSTEMLESS RESOLUTION — HARD RUNTIME CONTRACT]
This campaign has NO dice/check/rules resolver. `requires_check` is therefore not a playable
outcome and MUST NOT be emitted anywhere in action_sequence.
- Ordinary speech to a present addressed NPC is conversation, not an action check. Use
  resolution=conversation with an empty action_sequence unless the same player input also commits
  to a real physical/world action.
- For mundane committed actions use auto_success only when the fiction makes that safe.
- For uncertain/risky actions resolve the fiction directly as success/partial/failure/uncertain in
  the plan; never defer the turn to a nonexistent future check.
- A user-authored statement about the world is a premise/hypothesis, not objective canon. Do not
  create a fact, object, NPC or location merely because the player asserts that it exists or that an
  NPC said it. Require campaign context or a newly authorized world consequence.
- npc_introductions contains PHYSICALLY APPEARING CHARACTERS only. Symbols, clues, doors, objects,
  smells, lights, documents and other non-person entities are never NPC introductions.
- An unsolicited new NPC is a new complication. If the player did not directly seek contact with an
  unknown ordinary person, npc_introductions requires narration_policy.allow_new_complication=true
  with a concrete complication_source.
"""


def systemless_contract_issues(
    plan: CoordinatedTurnPlan,
    player_input: str,
) -> list[str]:
    """Return structural issues that cannot be executed by the current systemless runtime."""
    issues: list[str] = []

    if any(step.resolution == "requires_check" for step in plan.action_sequence.steps):
        issues.append(
            "systemless runtime has no check resolver: never emit requires_check; resolve the "
            "fiction directly or use conversation for ordinary NPC dialogue"
        )

    text = " ".join((player_input or "").split()).casefold()
    direct_contact = TurnAuthorityPlanner._matches_any(  # noqa: SLF001 - same contract owner
        TurnAuthorityPlanner.CONTACT_INTENT_PATTERNS,
        text,
    ) or bool(_DIRECT_CONTACT_RU_RE.search(text))
    complication_authorized = bool(
        plan.narration_policy.allow_new_complication
        and plan.narration_policy.complication_source
    )
    if plan.npc_introductions and not direct_contact and not complication_authorized:
        issues.append(
            "new physical NPC introductions are not authorized by this input: the player did not "
            "seek an unknown contact and no new complication was authorized; do not turn a player "
            "premise, object, symbol or clue into a character"
        )

    return issues


def _has_addressed_character(context_messages) -> bool:
    return any(
        "[INPUT ROUTING — authoritative]" in str(getattr(message, "content", ""))
        and "Addressed character:" in str(getattr(message, "content", ""))
        for message in context_messages
    )


def _is_plain_addressed_conversation(
    plan: CoordinatedTurnPlan,
    player_input: str,
) -> bool:
    """Recognize the narrow Round-28 failure without swallowing mixed physical actions.

    The Planner has already typed every step. We normalize only a clearly conversational latest
    input whose entire sequence consists of local `interaction` steps with no physical transition.
    Movement, observation/search, inventory, service and blocked/choice steps remain structured.
    """
    steps = list(plan.action_sequence.steps)
    if not steps or not _DIALOGUE_INPUT_RE.search(player_input or ""):
        return False
    if any(step.action_type != "interaction" for step in steps):
        return False
    if any(step.transition.required for step in steps):
        return False
    if any(step.resolution in {"requires_choice", "blocked"} for step in steps):
        return False
    return True


def normalize_addressed_conversation(
    plan: CoordinatedTurnPlan,
    player_input: str,
) -> CoordinatedTurnPlan:
    if not _is_plain_addressed_conversation(plan, player_input):
        return plan

    payload = plan.model_dump(mode="python")
    payload.update(
        {
            "resolution": "conversation",
            "action_sequence": ActionSequencePlan().model_dump(mode="python"),
            "scene_transition": SceneTransitionPlan().model_dump(mode="python"),
            # NPC-owned speech is intentionally not pre-authored as objective consequences.
            "observable_consequences": [],
        }
    )
    return CoordinatedTurnPlan.model_validate(payload)


def ensure_distinct_physical_location(source_location_id, resolved):
    """Reject a physical transition that resolves back onto its current Location identity."""
    if (
        resolved is not None
        and source_location_id is not None
        and resolved.id == source_location_id
    ):
        raise ValueError(
            "location_transition resolved to the current physical location; "
            "use stay/focus_transition instead of claiming physical travel"
        )
    return resolved


def detect_contained_repetition(
    candidate: str,
    previous_responses: list[str],
) -> RepetitionMatch | None:
    """Catch a long old response pasted inside a larger newly generated paragraph."""
    normalized_candidate = NarrationRepetitionGuard._normalized(candidate)  # noqa: SLF001
    if not normalized_candidate:
        return None
    for previous in previous_responses:
        normalized_previous = NarrationRepetitionGuard._normalized(previous)  # noqa: SLF001
        if len(normalized_previous) < 48:
            continue
        if (
            normalized_previous != normalized_candidate
            and normalized_previous in normalized_candidate
        ):
            return RepetitionMatch(
                previous_text=previous,
                similarity=1.0,
                exact=False,
            )
    return None


def install() -> None:
    """Install executable systemless invariants at the existing runtime guard boundary."""
    global _INSTALLED
    if _INSTALLED:
        return

    original_contract_issues = TurnAuthorityPlanner.contract_issues
    original_plan = TurnAuthorityPlanner.plan
    original_resolve_existing_location = SceneTransitionExecutor._resolve_existing_location
    original_repetition_detect = NarrationRepetitionGuard.detect

    if (
        "[SYSTEMLESS RESOLUTION — HARD RUNTIME CONTRACT]"
        not in TurnAuthorityPlanner.AUTHORITY_ADDENDUM
    ):
        TurnAuthorityPlanner.AUTHORITY_ADDENDUM += _SYSTEMLESS_PROMPT

    @classmethod
    def guarded_contract_issues(cls, plan, player_input):
        issues = list(original_contract_issues(plan, player_input))
        for issue in systemless_contract_issues(plan, player_input):
            if issue not in issues:
                issues.append(issue)
        return issues

    async def guarded_plan(self, selection, context_messages):
        plan = await original_plan(self, selection, context_messages)
        if not _has_addressed_character(context_messages):
            return plan
        player_input = self._latest_user_text(context_messages)  # noqa: SLF001
        return normalize_addressed_conversation(plan, player_input)

    async def reject_same_physical_location(
        self,
        campaign_id,
        source_location_id,
        destination,
    ):
        resolved = await original_resolve_existing_location(
            self,
            campaign_id,
            source_location_id,
            destination,
        )
        return ensure_distinct_physical_location(source_location_id, resolved)

    def repetition_with_containment(
        self,
        candidate,
        previous_responses,
        *,
        actor_turn,
    ):
        contained = detect_contained_repetition(candidate, previous_responses)
        if contained is not None:
            return contained
        return original_repetition_detect(
            self,
            candidate,
            previous_responses,
            actor_turn=actor_turn,
        )

    TurnAuthorityPlanner.contract_issues = guarded_contract_issues
    TurnAuthorityPlanner.plan = guarded_plan
    SceneTransitionExecutor._resolve_existing_location = reject_same_physical_location
    NarrationRepetitionGuard.detect = repetition_with_containment
    _INSTALLED = True


__all__ = [
    "detect_contained_repetition",
    "ensure_distinct_physical_location",
    "install",
    "normalize_addressed_conversation",
    "systemless_contract_issues",
]
