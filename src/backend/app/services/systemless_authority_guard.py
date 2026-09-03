from __future__ import annotations

import re

from app.models.turn import ChatMessage
from app.services.narration_repetition_guard import (
    NarrationRepetitionGuard,
    RepetitionMatch,
)
from app.services.scene_transition_executor import SceneTransitionExecutor
from app.services.turn_authority_planner import CoordinatedTurnPlan, TurnAuthorityPlanner
from app.services.turn_authority_service import TurnAuthorityService
from app.services.turn_planner import TurnPlanningError
from app.services.turn_runner import TurnRunner

_INSTALLED = False
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+|[\r\n]+")
_REFERENCE_ID_RE = re.compile(
    r"\[id=([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\]"
)

_SYSTEMLESS_PROMPT = """

[SYSTEMLESS RESOLUTION — STRUCTURAL RUNTIME CONTRACT]
This campaign has NO dice/check/rules resolver. `requires_check` is invalid and MUST NOT be emitted.
Planner owns semantic interpretation. Runtime does not infer dialogue, movement, contact, choices,
objects or NPC identity from words, stems, punctuation or capitalization.
- Use addressed_response_requested for selected-NPC response ownership.
- Mixed input may contain world actions plus speech; only world actions belong in action_sequence.
- Resolve uncertainty directly into current fiction instead of deferring to a future check.
- New physical NPCs must be typed in npc_introductions by Planner.
- Inventory operations must obey the authoritative structured ids: take only an object physically
  present here; drop/place/give only a player-owned item; give only to a physically present character.
"""


def input_uses_addressed_character(player_input: str) -> bool:
    """Keep selected-listener provenance; semantic ownership is decided by Planner later."""
    del player_input
    return True


def addressed_response_requested(
    player_input: str,
    plan: CoordinatedTurnPlan | None,
) -> bool:
    """Read model-authored response ownership instead of classifying the input lexically."""
    del player_input
    return bool(plan and plan.addressed_response_requested)


def sanitize_player_premise_npc_introductions(
    plan: CoordinatedTurnPlan,
    player_input: str,
) -> CoordinatedTurnPlan:
    """Compatibility entrypoint; Planner owns person/object semantics."""
    del player_input
    return plan


def _reference_ids(
    context_messages,
    prefix: str,
) -> tuple[bool, set[str]]:
    """Read exact UUID references from machine-generated authoritative context lines."""
    found = False
    ids: set[str] = set()
    for message in context_messages:
        for line in message.content.splitlines():
            if not line.startswith(prefix):
                continue
            found = True
            ids.update(match.casefold() for match in _REFERENCE_ID_RE.findall(line))
    return found, ids


def structured_inventory_contract_issues(
    plan: CoordinatedTurnPlan,
    context_messages,
) -> list[str]:
    """Reject inventory steps that contradict machine-generated authoritative entity ids.

    This intentionally does not interpret player prose. The context providers serialize database
    identity into fixed `Player-owned items`, `Objects physically here` and present-character lines;
    this guard only checks typed plan UUIDs against those exact sets.
    """
    has_owned, owned_ids = _reference_ids(context_messages, "Player-owned items:")
    has_objects, object_ids = _reference_ids(context_messages, "Objects physically here:")
    has_characters, character_ids = _reference_ids(
        context_messages,
        "Physically present characters:",
    )
    if not (has_owned and has_objects and has_characters):
        return []

    issues: list[str] = []
    for index, step in enumerate(plan.action_sequence.steps, start=1):
        if (
            step.action_type != "inventory"
            or step.resolution != "auto_success"
            or step.item_id is None
            or step.inventory_operation is None
        ):
            continue

        item_id = str(step.item_id).casefold()
        operation = step.inventory_operation

        if operation == "take" and item_id not in object_ids:
            if item_id in owned_ids:
                reason = (
                    "item is already player-owned; take is invalid here. "
                    "Use drop/place/give only if that matches the latest human input"
                )
            else:
                reason = "item is not an object physically present in the current scene"
            issues.append(
                f"inventory step {index}: take item_id={item_id} rejected because {reason}"
            )
            continue

        if operation in {"drop", "place", "give"} and item_id not in owned_ids:
            location = (
                "it is physically present in the scene instead"
                if item_id in object_ids
                else "it is not listed as player-owned"
            )
            issues.append(
                f"inventory step {index}: {operation} item_id={item_id} rejected because {location}"
            )

        if operation == "give":
            target_id = (
                str(step.inventory_target_id).casefold()
                if step.inventory_target_id is not None
                else ""
            )
            if not target_id or target_id not in character_ids:
                issues.append(
                    f"inventory step {index}: give target_id={target_id or 'missing'} rejected "
                    "because the target is not a physically present character"
                )

    return issues


def systemless_contract_issues(
    plan: CoordinatedTurnPlan,
    player_input: str,
    *,
    addressed_character: bool = False,
) -> list[str]:
    """Return only structural invariants the deterministic runtime can prove."""
    del player_input, addressed_character
    if any(step.resolution == "requires_check" for step in plan.action_sequence.steps):
        return [
            "systemless runtime has no check resolver: requires_check is structurally invalid"
        ]
    return []


def normalize_addressed_response(
    plan: CoordinatedTurnPlan,
    player_input: str,
) -> CoordinatedTurnPlan:
    """Compatibility entrypoint; Planner already separated speech ownership from world actions."""
    del player_input
    return plan


def normalize_addressed_conversation(
    plan: CoordinatedTurnPlan,
    player_input: str,
) -> CoordinatedTurnPlan:
    del player_input
    return plan


def ensure_distinct_physical_location(source_location_id, resolved):
    """Reject a transition that resolves to the exact current structured Location identity."""
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
    """Catch a long old response pasted inside a larger newly generated response."""
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


def detect_self_repetition(candidate: str) -> RepetitionMatch | None:
    """Catch duplicated sentence/paragraph blocks inside one generated response."""
    seen: dict[str, str] = {}
    for part in _SENTENCE_SPLIT_RE.split(candidate or ""):
        clean = part.strip()
        normalized = NarrationRepetitionGuard._normalized(clean)  # noqa: SLF001
        if len(normalized) < 48:
            continue
        previous = seen.get(normalized)
        if previous is not None:
            return RepetitionMatch(previous_text=previous, similarity=1.0, exact=True)
        seen[normalized] = clean
    return None


def install() -> None:
    """Install structural systemless invariants without lexical semantic classifiers."""
    global _INSTALLED
    if _INSTALLED:
        return

    original_contract_issues = TurnAuthorityPlanner.contract_issues
    original_plan = TurnAuthorityPlanner.plan
    original_resolve_existing_location = SceneTransitionExecutor._resolve_existing_location
    original_repetition_detect = NarrationRepetitionGuard.detect
    original_authority_build = TurnAuthorityService.build
    original_addressed_character_id = TurnRunner._addressed_character_id

    if (
        "[SYSTEMLESS RESOLUTION — STRUCTURAL RUNTIME CONTRACT]"
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

    async def guarded_plan(
        self,
        selection,
        context_messages,
        *,
        latest_user_input=None,
    ):
        player_input = latest_user_input or self._latest_user_text(context_messages)
        plan = await original_plan(
            self,
            selection,
            context_messages,
            latest_user_input=latest_user_input,
        )
        issues = structured_inventory_contract_issues(plan, context_messages)
        if not issues:
            return plan

        repair_context = [
            *context_messages,
            ChatMessage(
                role="user",
                content=(
                    "[DETERMINISTIC INVENTORY CONTRACT REJECTION]\n"
                    "The previous typed plan contradicts machine-generated authoritative inventory "
                    "ids and was rejected before any world-state mutation. Repair ONLY the listed "
                    "inventory hand-off problems while preserving the latest human intent. Do not "
                    "invent a different action merely to satisfy the guard.\n"
                    "Problems:\n- "
                    + "\n- ".join(issues)
                    + "\nRejected plan:\n"
                    + plan.model_dump_json()
                    + "\nReturn a complete replacement plan. For the same item, choose take only "
                    "when its id is in Objects physically here; choose drop/place/give only when "
                    "its id is in Player-owned items. The actual operation must still match the "
                    "latest human input semantically."
                ),
            ),
        ]
        repaired = await original_plan(
            self,
            selection,
            repair_context,
            latest_user_input=player_input,
        )
        remaining = structured_inventory_contract_issues(repaired, context_messages)
        if remaining:
            raise TurnPlanningError(
                "planner hand-off remained structurally invalid after inventory repair: "
                + "; ".join(remaining)
            )
        return repaired

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
        self_repeated = detect_self_repetition(candidate)
        if self_repeated is not None:
            return self_repeated
        contained = detect_contained_repetition(candidate, previous_responses)
        if contained is not None:
            return contained
        return original_repetition_detect(
            self,
            candidate,
            previous_responses,
            actor_turn=actor_turn,
        )

    async def response_owned_authority(self, *args, **kwargs):
        authority = await original_authority_build(self, *args, **kwargs)
        # Explicit actor-scoped internal callers remain authoritative. Public /talk only supplies
        # listener provenance; the typed Planner field decides whether that listener owns a response.
        if kwargs.get("acting_character_id") is not None or not authority.acting_character_id:
            return authority
        player_input = str(kwargs.get("player_input") or authority.player_input or "")
        plan = kwargs.get("plan")
        if addressed_response_requested(player_input, plan):
            return authority

        planned_disposition = plan.scene_disposition if plan is not None else "stay"
        update = {
            "acting_character_id": None,
            "acting_character_name": None,
        }
        if authority.scene_disposition == "actor_turn":
            update["scene_disposition"] = planned_disposition
            if planned_disposition == "stay":
                update["transition_type"] = "none"
        return authority.model_copy(update=update)

    def routed_addressed_character_id(turn_create):
        # Keep structured input-routing provenance intact. Do not infer from the utterance whether a
        # sticky listener is semantically addressed; Planner decides that after seeing full context.
        return original_addressed_character_id(turn_create)

    async def actor_neutral_narrator_context(
        self,
        *,
        compiler,
        campaign_id,
        turn_create,
        scene_id,
        max_budget_override,
    ):
        # Selected listener is response provenance, not a request to compile an actor-only context.
        messages, metadata = await compiler.compile_context(
            campaign_id=campaign_id,
            acting_character_id=None,
            scene_id=scene_id,
            current_user_content=turn_create.content,
            max_budget_override=max_budget_override,
        )
        return self._reserve_current_user(messages, metadata, turn_create.content)

    TurnAuthorityPlanner.contract_issues = guarded_contract_issues
    TurnAuthorityPlanner.plan = guarded_plan
    SceneTransitionExecutor._resolve_existing_location = reject_same_physical_location
    NarrationRepetitionGuard.RECENT_LIMIT = max(NarrationRepetitionGuard.RECENT_LIMIT, 12)
    NarrationRepetitionGuard.detect = repetition_with_containment
    TurnAuthorityService.build = response_owned_authority
    TurnRunner._addressed_character_id = staticmethod(routed_addressed_character_id)
    TurnRunner._recompile_narrator_context = actor_neutral_narrator_context
    _INSTALLED = True


__all__ = [
    "addressed_response_requested",
    "detect_contained_repetition",
    "detect_self_repetition",
    "ensure_distinct_physical_location",
    "input_uses_addressed_character",
    "install",
    "normalize_addressed_conversation",
    "normalize_addressed_response",
    "sanitize_player_premise_npc_introductions",
    "structured_inventory_contract_issues",
    "systemless_contract_issues",
]
