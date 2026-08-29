from __future__ import annotations

import json
from uuid import UUID

from pydantic import ConfigDict, Field

from app import config
from app.models.narration_validation import NarrationValidationResult
from app.models.turn import ChatMessage
from app.providers.llm_provider import LLMProviderError
from app.services.role_model_router import RoleModelSelection
from app.services.turn_authority_planner import CoordinatedTurnPlan, TurnAuthorityPlanner
from app.services.turn_authority_validator import TurnAuthorityValidator

_INSTALLED = False


class SemanticCoordinatedTurnPlan(CoordinatedTurnPlan):
    """Planner result with explicit model-owned response routing.

    The field replaces lexical guesses such as "contains a question mark" or verb lists. It is
    intentionally authored by Planner because deciding whether the latest human input addresses an
    NPC is a semantic decision, not a string-classification invariant.
    """

    model_config = ConfigDict(extra="ignore")

    addressed_response_requested: bool = False
    response_ownership_reason: str | None = Field(default=None, max_length=500)


_PLANNER_SEMANTIC_CONTRACT = """

[SEMANTIC AUTHORITY — NO LEXICAL HEURISTICS]
You are responsible for semantic classification. Runtime will NOT infer meaning from Russian verb
stems, question marks, emotion word lists, sensory word lists, or hand-maintained contact patterns.

Additional required field:
- addressed_response_requested: boolean. Set true only when the latest human input actually asks,
  tells, addresses, or otherwise expects a response from the selected addressed character. It may be
  true together with a world action sequence (for example: the player opens a file and asks the
  clerk a question). It must be false for a pure world action merely because /talk is still sticky.
- response_ownership_reason: one short semantic reason for that boolean.

Systemless resolution is absolute:
- requires_check is not a legal plan result. There is no dice/check resolver. Resolve genuine
  uncertainty directly into the fiction (success, partial success, failure, uncertain consequence)
  instead of postponing it to a nonexistent check.
- Spoken/addressed interaction belongs to response ownership, not action_sequence. In mixed input,
  put only actual world actions into action_sequence and use addressed_response_requested=true for
  the NPC response.

NPC/world authority is semantic too:
- If the resolved world response physically introduces a previously unknown person, that person MUST
  be present in npc_introductions. Narrator must never invent the body later.
- Do not introduce a person merely because an object, clue, door, symbol, smell or location noun was
  mentioned. Decide from meaning and context, not capitalization or vocabulary lists.
- A newly appearing person is allowed only when justified by the player's actual contact/action or by
  an established complication source. Otherwise keep them out of the physical scene.
- Explicit player movement requires structured movement authority unless the world actually blocks
  it. Decide whether the input commits to movement from its meaning, not from a verb lookup table.
- Preserve unresolved human choices semantically. Never choose a branch for the protagonist.
"""


_NARRATION_REVIEW_PROMPT = """[SEMANTIC NARRATION AUTHORITY REVIEW]
You are the final semantic reviewer of a proposed validator verdict. Do not continue the story and do
not rewrite prose. Judge candidate prose against TURN AUTHORITY from meaning and grammatical roles,
not keyword lists.

The previous validator may be wrong. Re-evaluate every alleged violation from scratch.

Critical ownership rules:
- PLAYER AGENCY exists only when prose actually assigns the human protagonist new voluntary speech,
  choice, decision, plan, belief, consent, emotion, intention or next action beyond player_input.
- Physical perception is not automatically an authored emotion or thought. Seeing, hearing, smelling,
  tasting, touch, temperature, pain, pressure, balance and other immediate perception are allowed when
  grounded by the scene. Classify by meaning in context; never use a vocabulary whitelist/blacklist.
- Thoughts, feelings, speech, facial expressions, gestures, posture and conversational behavior of a
  present/authorized NPC belong to that NPC, not to the protagonist.
- A present response actor may answer the current question naturally and may state personal claims,
  memories, observations, opinions, uncertainty or lies. Such speech is epistemic character_claim,
  not objective world canon merely because it contains new information.
- Evidence for player_agency must quote the shortest exact fragment that actually belongs to the
  protagonist. Never cite an NPC-owned fragment as protagonist agency.

World authority rules:
- A physically new NPC, route, threat, clue, significant object, completed movement or objective
  world outcome still needs typed authority. Literary quality is not permission to mutate canon.
- Neutral scene texture and sensory staging are allowed when they do not create a significant fact.
- allowed_new_npcs and allowed_existing_npc_arrivals are authoritative physical permissions.

Return exactly the NarrationValidationResult schema. If the candidate is legal, return pass with an
empty violations list even when the previous validator claimed many errors. All human-readable fields
must be Russian.
"""


def _structural_contract_issues(
    cls,
    plan: CoordinatedTurnPlan,
    player_input: str,
) -> list[str]:
    """Keep only machine-provable planner invariants at the deterministic boundary."""
    del cls, player_input
    if any(step.resolution == "requires_check" for step in plan.action_sequence.steps):
        return [
            (
                "systemless runtime has no check resolver: requires_check is structurally invalid; "
                "resolve the fictional uncertainty directly"
            )
        ]
    return []


def _no_contact_autocreation(cls, plan, player_input):
    """Planner, not a noun/regex table, owns whether a new physical person exists."""
    del cls, player_input
    return plan


def _systemless_structural_issues(
    plan: CoordinatedTurnPlan,
    player_input: str,
    *,
    addressed_character: bool = False,
) -> list[str]:
    del player_input, addressed_character
    if any(step.resolution == "requires_check" for step in plan.action_sequence.steps):
        return [
            "systemless runtime has no check resolver: requires_check is structurally invalid"
        ]
    return []


def _preserve_semantic_plan(plan, player_input):
    """Do not delete/reshape plan steps by matching speech verbs. Planner already classified them."""
    del player_input
    return plan


def _addressed_response_requested(player_input: str, plan: CoordinatedTurnPlan | None) -> bool:
    del player_input
    if plan is None:
        return False
    explicit = getattr(plan, "addressed_response_requested", None)
    if explicit is not None:
        return bool(explicit)
    return plan.resolution == "conversation"


def _transport_has_addressee(player_input: str) -> bool:
    """Sticky /talk is transport context; Planner later decides whether it owns this response."""
    del player_input
    return True


def _structured_addressee(turn_create) -> UUID | None:
    snapshot = turn_create.context_snapshot
    if not isinstance(snapshot, dict):
        return None
    routing = snapshot.get("input_routing")
    if not isinstance(routing, dict):
        return None
    value = routing.get("addressed_character_id")
    try:
        return UUID(str(value)) if value else None
    except (TypeError, ValueError):
        return None


def _identity_ownership(result, authority, candidate_text):
    """Compatibility hook: semantic ownership is decided by the validator model, not regexes."""
    del authority, candidate_text
    return result


def _identity_actor_protection(authority, result, candidate_text=""):
    """Compatibility hook: actor attribution is part of semantic review, not marker lists."""
    del authority, candidate_text
    return result


def _identity_deterministic_semantics(cls, result, authority, candidate_text):
    del cls, authority, candidate_text
    return result


async def _semantic_generate_plan(
    self,
    selection: RoleModelSelection,
    messages: list[ChatMessage],
) -> SemanticCoordinatedTurnPlan:
    data = await self._router.generate_json(
        self._provider,
        selection,
        messages,
        max_tokens=max(config.settings.PLANNER_MAX_TOKENS, 1250),
        temperature=config.settings.PLANNER_TEMPERATURE,
        response_model=SemanticCoordinatedTurnPlan,
    )
    return SemanticCoordinatedTurnPlan.model_validate(data)


async def _semantic_review_failed_narration(
    validator: TurnAuthorityValidator,
    selection: RoleModelSelection | None,
    authority,
    candidate_text: str,
    previous: NarrationValidationResult,
) -> NarrationValidationResult:
    messages = [
        ChatMessage(role="system", content=_NARRATION_REVIEW_PROMPT),
        ChatMessage(
            role="user",
            content=(
                "[TURN AUTHORITY]\n"
                + json.dumps(authority.validator_payload(), ensure_ascii=False, indent=2)
                + "\n\n[CANDIDATE NARRATION]\n"
                + candidate_text
                + "\n\n[PREVIOUS VERDICT — MAY BE WRONG]\n"
                + previous.model_dump_json()
            ),
        ),
    ]
    data = await validator._router.generate_json(
        validator._provider,
        selection,
        messages,
        max_tokens=min(config.settings.NARRATION_VALIDATOR_MAX_TOKENS, 700),
        temperature=0.0,
        response_model=NarrationValidationResult,
    )
    reviewed = NarrationValidationResult.model_validate(data)

    # Deterministic post-checks remain only for formal, machine-provable surface invariants.
    reviewed = validator.apply_deterministic_authority(reviewed, authority)
    reviewed = validator.apply_deterministic_language(reviewed, authority, candidate_text)
    return validator.apply_deterministic_surface_quality(reviewed, candidate_text)


def install() -> None:
    """Move semantic authority decisions from lexical heuristics to typed LLM agents."""
    global _INSTALLED
    if _INSTALLED:
        return

    import app.services.actor_turn_authority_guard as actor_guard
    import app.services.narrator_quality_recovery_guard as quality_guard
    import app.services.systemless_authority_guard as systemless_guard
    from app.services.action_sequence_executor import ActionSequenceExecutor
    from app.services.turn_runner import TurnRunner

    # Keep transport and execution invariants deterministic, but stop inferring semantics from text.
    systemless_guard.input_uses_addressed_character = _transport_has_addressee
    systemless_guard.addressed_response_requested = _addressed_response_requested
    systemless_guard.normalize_addressed_response = _preserve_semantic_plan
    systemless_guard.normalize_addressed_conversation = _preserve_semantic_plan
    systemless_guard.sanitize_player_premise_npc_introductions = _preserve_semantic_plan
    systemless_guard.systemless_contract_issues = _systemless_structural_issues

    TurnRunner._addressed_character_id = staticmethod(_structured_addressee)
    TurnAuthorityPlanner.contract_issues = classmethod(_structural_contract_issues)
    TurnAuthorityPlanner.normalize_affirmative_direct_contact = classmethod(_no_contact_autocreation)
    TurnAuthorityPlanner._generate_plan = _semantic_generate_plan

    # Remove deterministic semantic ownership/movement decisions. Formal surface checks stay active.
    quality_guard.apply_narrator_ownership = _identity_ownership
    actor_guard.protect_actor_turn_validation = _identity_actor_protection
    TurnAuthorityValidator.apply_deterministic_player_agency = classmethod(
        _identity_deterministic_semantics
    )
    TurnAuthorityValidator.apply_deterministic_actor_agency = classmethod(
        _identity_deterministic_semantics
    )
    TurnAuthorityValidator.apply_deterministic_movement_surface = classmethod(
        _identity_deterministic_semantics
    )

    if "[SEMANTIC AUTHORITY — NO LEXICAL HEURISTICS]" not in TurnAuthorityPlanner.AUTHORITY_ADDENDUM:
        TurnAuthorityPlanner.AUTHORITY_ADDENDUM += _PLANNER_SEMANTIC_CONTRACT

    current_validate = TurnAuthorityValidator.validate

    async def semantically_adjudicated_validate(self, selection, authority, candidate_text):
        result = await current_validate(self, selection, authority, candidate_text)
        if result.verdict == "pass":
            return result
        try:
            return await _semantic_review_failed_narration(
                self,
                selection,
                authority,
                candidate_text,
                result,
            )
        except (LLMProviderError, ValueError, TypeError):
            # A failed reviewer must not silently bless prose. Preserve the first semantic verdict;
            # the existing preserve-first repair/fallback path remains the containment boundary.
            return result

    TurnAuthorityValidator.validate = semantically_adjudicated_validate

    original_execute = ActionSequenceExecutor.execute

    async def reject_escaped_checks(self, campaign_id, source_scene_id, trigger_turn_id, plan, **kwargs):
        if any(step.resolution == "requires_check" for step in plan.steps):
            raise ValueError(
                "systemless planner contract violation: requires_check escaped semantic planning"
            )
        return await original_execute(
            self,
            campaign_id,
            source_scene_id,
            trigger_turn_id,
            plan,
            **kwargs,
        )

    ActionSequenceExecutor.execute = reject_escaped_checks
    _INSTALLED = True


__all__ = [
    "SemanticCoordinatedTurnPlan",
    "install",
]
