from __future__ import annotations

import re
from contextvars import ContextVar

from app.services.narration_repetition_guard import (
    NarrationRepetitionGuard,
    RepetitionMatch,
)
from app.services.player_intent_contract import has_unresolved_choice
from app.services.scene_transition_executor import SceneTransitionExecutor
from app.services.turn_authority_planner import CoordinatedTurnPlan, TurnAuthorityPlanner
from app.services.turn_authority_service import TurnAuthorityService
from app.services.turn_planner import ActionSequencePlan, SceneTransitionPlan
from app.services.turn_runner import TurnRunner

_INSTALLED = False
_ADDRESSED_PLANNER_CONTEXT: ContextVar[bool] = ContextVar(
    "systemless_addressed_planner_context",
    default=False,
)

_DIALOGUE_INPUT_RE = re.compile(
    r"(?:\?|^\s*(?:кто|что|где|когда|почему|зачем|как|"
    r"расскаж\w*|скаж\w*|объясн\w*|помн\w*|можете|"
    r"who|what|where|when|why|how|tell\b|explain\b|do\s+you\b))",
    flags=re.IGNORECASE,
)
_DIALOGUE_STEP_RE = re.compile(
    r"\b(?:спрос\w*|уточн\w*|сказ\w*|говор\w*|расскаж\w*|объясн\w*|"
    r"ответ\w*|обращ\w*|ask\w*|tell\w*|say\w*|speak\w*|question\w*)\b",
    flags=re.IGNORECASE,
)
_ACTION_INPUT_RE = re.compile(
    r"\b(?:иду|еду|пойду|поеду|выхожу|ухожу|направляюсь|отправляюсь|"
    r"возвращаюсь|перехожу|подхожу|отхожу|покидаю|захожу|вхожу|"
    r"осматриваю|обыскиваю|беру|кладу|открываю|закрываю|ищу|проверяю|"
    r"делаю|пытаюсь|жду|сплю|отдыхаю|go|going|leave|leaving|return|"
    r"returning|enter|entering|head|heading|inspect|search|take|open|close|wait)\b",
    flags=re.IGNORECASE,
)
_EXPLICIT_SPEECH_RE = re.compile(
    r"\b(?:спрашиваю|спрошу|говорю|скажу|уточняю|обращаюсь|"
    r"ask|asking|tell|telling|say|saying|speak|speaking)\b",
    flags=re.IGNORECASE,
)
_VOCATIVE_RE = re.compile(r"(?:^|[.!?]\s+)[А-ЯЁ][а-яё-]{2,}\s*,")
_CLAUSE_RE = re.compile(r"(?<=[.!?])\s+")
_DIRECT_CONTACT_RU_RE = re.compile(
    rf"\b(?:расспраш\w*|спрашива\w*)\s+"
    rf"(?:[^.!?]{{0,24}}\s)?{TurnAuthorityPlanner.GENERIC_CONTACT_ROLE_RU}\b",
    flags=re.IGNORECASE,
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+|[\r\n]+")

_SYSTEMLESS_PROMPT = """

[SYSTEMLESS RESOLUTION — HARD RUNTIME CONTRACT]
This campaign has NO dice/check/rules resolver. `requires_check` is therefore not a playable
outcome and MUST NOT be emitted anywhere in action_sequence.
- Ordinary speech to a present addressed NPC is conversation, not an action check or a player
  choice. The NPC's reply is RESPONSE OWNERSHIP, not a player action_sequence step.
- Mixed input may contain world actions plus speech. Put only the world actions into action_sequence;
  never encode "ask/tell the addressed NPC" as requires_check/requires_choice/blocked. The addressed
  NPC may answer after the structured world actions finish.
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


def _is_addressed_dialogue_step(step) -> bool:
    """Return whether a structured interaction is actually speech to the selected listener."""
    return bool(
        step.action_type == "interaction"
        and not step.transition.required
        and _DIALOGUE_STEP_RE.search(step.intent or "")
    )


def input_uses_addressed_character(player_input: str) -> bool:
    """Distinguish sticky listener state from ownership of the current response.

    `/talk` is intentionally sticky for normal dialogue, but a new pure action/movement input is not
    silently converted into an NPC turn. Mixed inputs remain addressed when they contain an explicit
    speech clause after/beside the action.
    """
    text = " ".join((player_input or "").split()).strip()
    if not text:
        return False
    if not _ACTION_INPUT_RE.search(text):
        return True
    if _EXPLICIT_SPEECH_RE.search(text) or _VOCATIVE_RE.search(text):
        return True
    for clause in _CLAUSE_RE.split(text):
        if clause.rstrip().endswith("?") and not _ACTION_INPUT_RE.search(clause):
            return True
    return False


def addressed_response_requested(
    player_input: str,
    plan: CoordinatedTurnPlan | None,
) -> bool:
    if plan is not None and plan.resolution == "conversation":
        return True
    return input_uses_addressed_character(player_input)


def _direct_contact_requested(player_input: str) -> bool:
    text = " ".join((player_input or "").split()).casefold()
    return TurnAuthorityPlanner._matches_any(  # noqa: SLF001 - same contract owner
        TurnAuthorityPlanner.CONTACT_INTENT_PATTERNS,
        text,
    ) or bool(_DIRECT_CONTACT_RU_RE.search(text))


def _lowercase_literal_mention(label: str | None, player_input: str) -> bool:
    """Whether the player used this planner label as an ordinary lower-case premise noun.

    This is intentionally ontology-free: instead of maintaining an ever-growing list of doors,
    symbols, clues and other objects, we use the player's own casing as evidence that a literal
    phrase was not introduced as a proper-named character.
    """
    clean = " ".join((label or "").split()).strip()
    if len(clean) < 2:
        return False
    pattern = re.compile(rf"(?<!\w){re.escape(clean)}(?!\w)", re.IGNORECASE)
    for match in pattern.finditer(player_input or ""):
        mention = match.group(0)
        cased = [char for char in mention if char.isalpha()]
        if cased and mention == mention.lower():
            return True
    return False


def sanitize_player_premise_npc_introductions(
    plan: CoordinatedTurnPlan,
    player_input: str,
) -> CoordinatedTurnPlan:
    """Salvage a real structured action from an incidental object-as-NPC planner category error.

    Premise-only plans remain fail-closed: if there is no executable structured world action to
    preserve, the bogus npc_introduction stays in the plan so the existing Round-28 contract rejects
    it. For an otherwise valid compound sequence, a lower-case literal object copied from player
    input may be removed without throwing away the player's real actions. Explicit unknown contacts
    such as "расспрашиваю прохожего" remain eligible NPC introductions.
    """
    if not plan.npc_introductions:
        return plan

    # Do not convert the Round-28 premise boundary from hard reject into silent acceptance. Silent
    # normalization exists only to save a real structured action sequence from an incidental type
    # error in npc_introductions.
    if not plan.action_sequence.steps:
        return plan

    direct_contact = _direct_contact_requested(player_input)
    kept = []
    for introduction in plan.npc_introductions:
        literal_premise = any(
            _lowercase_literal_mention(label, player_input)
            for label in (introduction.canonical_name, introduction.role)
            if label
        )
        if not literal_premise:
            kept.append(introduction)
            continue

        contact_label = " ".join(
            value
            for value in (introduction.canonical_name, introduction.role)
            if value
        )
        generic_contact = bool(
            re.search(TurnAuthorityPlanner.GENERIC_CONTACT_ROLE_RU, contact_label, re.IGNORECASE)
            or re.search(TurnAuthorityPlanner.GENERIC_CONTACT_ROLE_EN, contact_label, re.IGNORECASE)
        )
        if direct_contact and generic_contact:
            kept.append(introduction)

    if len(kept) != len(plan.npc_introductions):
        plan.npc_introductions[:] = kept
    return plan


def systemless_contract_issues(
    plan: CoordinatedTurnPlan,
    player_input: str,
    *,
    addressed_character: bool = False,
) -> list[str]:
    """Return structural issues that cannot be executed by the current systemless runtime."""
    sanitize_player_premise_npc_introductions(plan, player_input)
    issues: list[str] = []

    invalid_checks = [
        step
        for step in plan.action_sequence.steps
        if step.resolution == "requires_check"
        and not (addressed_character and _is_addressed_dialogue_step(step))
    ]
    if invalid_checks:
        issues.append(
            "systemless runtime has no check resolver: never emit requires_check; resolve the "
            "fiction directly or use response ownership for ordinary NPC dialogue"
        )

    direct_contact = _direct_contact_requested(player_input)
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
    steps = list(plan.action_sequence.steps)
    if not steps or not _DIALOGUE_INPUT_RE.search(player_input or ""):
        return False
    if any(not _is_addressed_dialogue_step(step) for step in steps):
        return False
    if has_unresolved_choice(player_input):
        return False
    return True


def normalize_addressed_response(
    plan: CoordinatedTurnPlan,
    player_input: str,
) -> CoordinatedTurnPlan:
    """Remove NPC reply requests from player action execution without losing mixed actions.

    Planner sometimes encodes `ask Marina` as a requires_choice/check action. That is a category
    error: the player already chose to ask; only the NPC response remains. We therefore remove only
    speech-like addressed interaction steps. Physical/observation/inventory/movement steps remain
    structured and executable in their original order.
    """
    if has_unresolved_choice(player_input):
        return plan

    original_steps = list(plan.action_sequence.steps)
    dialogue_steps = [step for step in original_steps if _is_addressed_dialogue_step(step)]
    if not dialogue_steps:
        return plan

    remaining = [step for step in original_steps if not _is_addressed_dialogue_step(step)]
    removed_outcomes = {
        " ".join((step.observable_outcome or "").split())
        for step in dialogue_steps
        if step.observable_outcome
    }
    payload = plan.model_dump(mode="python")
    payload["action_sequence"] = ActionSequencePlan(
        summary=plan.action_sequence.summary,
        steps=remaining,
    ).model_dump(mode="python")
    payload["observable_consequences"] = [
        value
        for value in plan.observable_consequences
        if " ".join(value.split()) not in removed_outcomes
    ]

    if not remaining:
        payload.update(
            {
                "resolution": "conversation",
                "scene_transition": SceneTransitionPlan().model_dump(mode="python"),
                # NPC-owned reply content must not be pre-authored as objective consequences.
                "observable_consequences": [],
            }
        )
    else:
        payload["resolution"] = "sequence"
        # TurnPlan's validator rebuilds the focus boundary and sequence payload from remaining steps.
        payload["scene_transition"] = SceneTransitionPlan().model_dump(mode="python")

    return CoordinatedTurnPlan.model_validate(payload)


def normalize_addressed_conversation(
    plan: CoordinatedTurnPlan,
    player_input: str,
) -> CoordinatedTurnPlan:
    """Compatibility entrypoint retained for Round-28 tests/callers."""
    if _is_plain_addressed_conversation(plan, player_input):
        return normalize_addressed_response(plan, player_input)
    # Mixed addressed turns are now decomposed too instead of being collapsed wholesale.
    return normalize_addressed_response(plan, player_input)


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
    """Install executable systemless/response invariants at the existing runtime guard boundary."""
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
        "[SYSTEMLESS RESOLUTION — HARD RUNTIME CONTRACT]"
        not in TurnAuthorityPlanner.AUTHORITY_ADDENDUM
    ):
        TurnAuthorityPlanner.AUTHORITY_ADDENDUM += _SYSTEMLESS_PROMPT

    @classmethod
    def guarded_contract_issues(cls, plan, player_input):
        # Normalize a model category error before either the base or systemless contract can reject
        # an otherwise valid compound action as an unauthorized character introduction.
        sanitize_player_premise_npc_introductions(plan, player_input)
        issues = list(original_contract_issues(plan, player_input))
        addressed = _ADDRESSED_PLANNER_CONTEXT.get()
        for issue in systemless_contract_issues(
            plan,
            player_input,
            addressed_character=addressed,
        ):
            if issue not in issues:
                issues.append(issue)
        return issues

    async def guarded_plan(self, selection, context_messages):
        addressed = _has_addressed_character(context_messages)
        token = _ADDRESSED_PLANNER_CONTEXT.set(addressed)
        try:
            plan = await original_plan(self, selection, context_messages)
        finally:
            _ADDRESSED_PLANNER_CONTEXT.reset(token)
        if not addressed:
            return plan
        player_input = self._latest_user_text(context_messages)  # noqa: SLF001
        return normalize_addressed_response(plan, player_input)

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
        # Explicit actor-scoped internal callers remain authoritative. Public `/talk` reaches this
        # path with acting_character_id=None and a sticky addressee persisted on the user turn.
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
        addressed_id = original_addressed_character_id(turn_create)
        if addressed_id is None:
            return None
        return (
            addressed_id
            if input_uses_addressed_character(str(getattr(turn_create, "content", "") or ""))
            else None
        )

    async def actor_neutral_narrator_context(
        self,
        *,
        compiler,
        campaign_id,
        turn_create,
        scene_id,
        max_budget_override,
    ):
        # The selected `/talk` listener is not response authority. Typed TurnAuthority, injected
        # after structured execution, is the sole source of response ownership for Narrator.
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
    "systemless_contract_issues",
]
