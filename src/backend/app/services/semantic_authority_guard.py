from __future__ import annotations

import json

from app import config
from app.models.narration_validation import NarrationValidationResult
from app.models.turn import ChatMessage
from app.providers.llm_provider import LLMProviderError
from app.services.role_model_router import RoleModelSelection
from app.services.turn_authority_validator import TurnAuthorityValidator

_INSTALLED = False


_NARRATION_REVIEW_PROMPT = """[SEMANTIC NARRATION AUTHORITY REVIEW]
You are the final semantic reviewer of a proposed validator verdict. Do not continue the story and do
not rewrite prose. Judge candidate prose against TURN AUTHORITY from meaning, grammatical roles and
scene context. Never use keyword/stem whitelists or blacklists.

The previous validator may be wrong. Re-evaluate every alleged violation from scratch.

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

    # Only formal, machine-provable post-checks remain deterministic.
    reviewed = validator.apply_deterministic_authority(reviewed, authority)
    reviewed = validator.apply_deterministic_language(reviewed, authority, candidate_text)
    return validator.apply_deterministic_surface_quality(reviewed, candidate_text)


def install() -> None:
    """Add model-based re-adjudication for failed narration and a structural check boundary."""
    global _INSTALLED
    if _INSTALLED:
        return

    from app.services.action_sequence_executor import ActionSequenceExecutor

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
        if any(step.resolution == "requires_check" for step in plan.steps):
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
