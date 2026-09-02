from __future__ import annotations

import json

from app import config
from app.models.narration_validation import NarrationValidationResult
from app.models.turn import ChatMessage
from app.providers.llm_provider import LLMProviderError
from app.services.role_model_router import ModelRole, RoleModelSelection
from app.services.turn_authority_validator import TurnAuthorityValidator

_INSTALLED = False

_COMPLETENESS_CONTRACT = """

[ROUND 43 — COMPLETENESS AND SCENE-BOUND PRESENCE]
- TURN COMPLETENESS: the candidate must actually resolve the CURRENT player_input to the extent the
  typed authority already resolved it. If observable_consequences or a completed action sequence
  contain a current result, prose must communicate that result before moving to an ending hook.
  A response that only sets up an answer, stops at a cliffhanger, repeats the question, or promises
  that the result will be known next turn is incomplete and requires repair. Do not demand a result
  only when authority itself is waiting for pending_player_choice, a blocked prerequisite, or an
  explicitly unresolved current-world outcome.
- CONVERSATION COMPLETENESS: when an authorized acting_character owns the current response, the prose
  must contain that character's meaningful present response or an equally concrete present behavior
  that is itself the authorized result. A teaser that postpones the answer without authority is not
  sufficient.
- SCENE-BOUND NPC PRESENCE: after a location/time/focus boundary, a character who is only in the
  source scene and is absent from target present_characters/allowed arrivals/new NPCs cannot continue
  speaking, accompany the player, enter the destination, sit in the new room, or otherwise act there.
  Mentioning that absent character as memory, report, message, thought-about person, or quoted past
  speech is legal and does not materialize them.
"""

_NARRATION_REVIEW_PROMPT = """[SEMANTIC NARRATION AUTHORITY REVIEW]
You are an independent semantic reviewer of a proposed narration-validator verdict. Do not continue
the story and do not rewrite prose. Judge candidate prose against TURN AUTHORITY from meaning,
grammatical roles and scene context. Never use keyword/stem whitelists or blacklists.

The previous validator may be wrong. Re-evaluate every alleged violation from scratch; do not defer to
its count or wording.

Critical ownership rules:
- PLAYER AGENCY exists only when prose actually assigns the human protagonist new voluntary speech,
  choice, decision, plan, belief, consent, emotion, intention or next action beyond player_input.
- Physical perception is not automatically an authored emotion or thought. Seeing, hearing, smelling,
  tasting, touch, temperature, pain, pressure, balance and other immediate perception are allowed when
  grounded by the scene. Classify by meaning in context.
- Thoughts, feelings, speech, facial expressions, gestures, posture and conversational behavior of a
  present/authorized NPC belong to that NPC, not to the protagonist.
- A present response actor may answer the current question naturally and may state personal claims,
  memories, observations, opinions, uncertainty or lies. Such speech is epistemic character_claim,
  not objective world canon merely because it contains new information.
- When acting_character is set, verify speaker consistency semantically. New first-person NPC dialogue
  must belong to that actor, not be a copied/reassigned earlier line, self-reference, grammatical
  identity or conversational stance of a different NPC. Use speaker_consistency for this defect.
- Evidence for player_agency must quote the shortest exact fragment that actually belongs to the
  protagonist. Never cite an NPC-owned fragment as protagonist agency.

World authority rules:
- A physically new NPC, route, threat, clue, significant object, completed movement or objective
  world outcome still needs typed authority. Literary quality is not permission to mutate canon.
- Neutral scene texture and sensory staging are allowed when they do not create a significant fact.
- allowed_new_npcs and allowed_existing_npc_arrivals are authoritative physical permissions.
- After a scene boundary, source-scene NPCs absent from target presence/allowed arrivals cannot keep
  talking, follow the protagonist, enter the destination or otherwise physically act there. A remote
  mention or quotation of past speech is not physical presence.

Turn-completeness rules:
- The candidate must resolve the current player input to the extent TURN AUTHORITY already resolved
  it. If observable_consequences or completed action-sequence results exist, prose must communicate
  them in the current answer before any ending hook.
- A half-turn that only builds suspense, restates the attempt, or postpones an already-known result to
  the next response is a concrete violation. Do not demand completion when authority itself requires
  a pending player choice, is structurally blocked, or explicitly leaves the current outcome unknown.
- An authorized NPC response must be meaningfully present now; do not accept a fake cliffhanger that
  withholds an answer the authority has already assigned to this turn.

Player-facing surface rules:
- The answer must stay inside the fiction. Semantic commentary about an internal action causing no
  external state change, information being mechanically received, the player/response/narration, or
  waiting for a next turn is meta_language even when it contains no obvious debug token.
- Do not reject ordinary in-world narration merely because it says nothing changed; reject only when
  the text explains the game/engine abstraction instead of dramatizing the fictional moment.

Return exactly the NarrationValidationResult schema. If the candidate is legal, return pass with an
empty violations list even when the previous validator claimed many errors. Use speaker_consistency
and meta_language when those are the actual defects. All human-readable fields must be Russian.
"""


async def _semantic_review_failed_narration(
    validator: TurnAuthorityValidator,
    selection: RoleModelSelection | None,
    authority,
    candidate_text: str,
    previous: NarrationValidationResult,
) -> NarrationValidationResult:
    review_selection = await validator._router.resolve(
        authority.campaign_id,
        ModelRole.EVALUATOR,
    )
    review_selection = review_selection or selection
    if review_selection is None:
        return previous

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
        review_selection,
        messages,
        max_tokens=min(config.settings.NARRATION_VALIDATOR_MAX_TOKENS, 700),
        temperature=0.0,
        response_model=NarrationValidationResult,
    )
    reviewed = NarrationValidationResult.model_validate(data)

    # Only formal, machine-provable post-checks remain deterministic.
    reviewed = validator.apply_deterministic_authority(reviewed, authority)
    reviewed = validator.apply_deterministic_language(reviewed, authority, candidate_text)
    return validator.apply_deterministic_surface_quality(reviewed, candidate_text)


def install() -> None:
    """Add independent model re-adjudication and a structural executor defense."""
    global _INSTALLED
    if _INSTALLED:
        return

    from app.services.action_sequence_executor import ActionSequenceExecutor

    if _COMPLETENESS_CONTRACT not in TurnAuthorityValidator.SYSTEM_PROMPT:
        TurnAuthorityValidator.SYSTEM_PROMPT += _COMPLETENESS_CONTRACT

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
            # existing preserve-first repair/fallback remains the containment boundary.
            return result

    TurnAuthorityValidator.validate = semantically_adjudicated_validate

    original_execute = ActionSequenceExecutor.execute

    async def reject_escaped_checks(
        self,
        campaign_id,
        source_scene_id,
        trigger_turn_id,
        plan,
        **kwargs,
    ):
        if any(getattr(step, "resolution", None) == "requires_check" for step in plan.steps):
            raise ValueError(
                "systemless planner contract violation: requires_check escaped typed planning"
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


__all__ = ["install"]