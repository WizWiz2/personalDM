from __future__ import annotations

import json

from app.config import settings
from app.models.narration_validation import NarrationValidationResult
from app.models.turn import ChatMessage
from app.models.turn_authority import TurnAuthority
from app.providers.llm_provider import LLMProvider, LLMProviderError
from app.services.llm_usage_tracker import record_decision
from app.services.narration_validator import NarrationValidationError
from app.services.role_model_router import ModelRole, RoleModelRouter, RoleModelSelection


FOUR_BANS = frozenset({"absent_character", "canon_conflict", "player_agency", "invalid_movement"})


def four_bans_only(result: NarrationValidationResult) -> NarrationValidationResult:
    """Keep only errors typed as one of the four bans; anything else is a note, not a failure."""
    violations = [
        item if item.violation_type in FOUR_BANS or item.severity != "error"
        else item.model_copy(update={"severity": "warning"})
        for item in result.violations
    ]
    errors = [item for item in violations if item.severity == "error"]
    return NarrationValidationResult(
        verdict="repair_required" if errors else "pass",
        summary=result.summary,
        violations=violations,
    )


class TurnAuthorityValidator:
    """One control-model call that judges prose against the four bans of a typed TurnAuthority.

    What is not forbidden is allowed. The typed facts come from ``TurnAuthority.validator_payload``;
    there are no deterministic prose heuristics, coverage checks or second reviews here.
    """

    SYSTEM_PROMPT = """[FOUR BANS NARRATION CHECK]
You never continue the story. You receive the typed FACTS of one turn and candidate prose.
Everything that is not banned below is allowed. Return repair_required ONLY for these four bans:

1. absent_character: a person is physically here now (appears, acts, speaks in the scene, is
   touched or seen here) but is not in present_characters, allowed_new_npcs or
   allowed_existing_npc_arrivals. MENTIONING someone who is not here is allowed: names in
   dialogue, memories, rumours, people somewhere else.
2. canon_conflict: prose denies or overwrites an established_state entry or the result of an
   executed step (a completed step happened, a blocked step did not).
3. player_agency: prose gives the player character new speech, decisions, thoughts, feelings or
   voluntary actions beyond player_input. Rendering player_input itself, its result, and what the
   player character perceives is allowed.
4. invalid_movement: prose moves anyone to another place without a typed trip in
   scene_disposition/transition_type. Moving inside the current place is allowed.

Present characters may speak, answer, refuse, stay silent, gesture, move inside the place, share
opinions, claims or new information. None of that is required and none of it is a violation.
Style, completeness, pacing, atmosphere and how a question is answered are not your concern.

For every error, evidence is the shortest exact fragment of the candidate and correction is a
short prose-only fix in Russian. Return exactly:
{
  "verdict": "pass|repair_required",
  "summary": "short reason in Russian",
  "violations": [
    {
      "violation_type": "absent_character|canon_conflict|player_agency|invalid_movement",
      "severity": "error",
      "evidence": "shortest exact candidate fragment",
      "correction": "specific prose-only correction in Russian"
    }
  ]
}
"""

    def __init__(self, router: RoleModelRouter):
        self._router = router
        self._provider = LLMProvider()

    @property
    def telemetry(self) -> dict:
        return dict(self._provider.last_telemetry or {})

    async def validate(
        self,
        selection: RoleModelSelection,
        authority: TurnAuthority,
        candidate_text: str,
    ) -> NarrationValidationResult:
        role = ModelRole.NARRATION_VALIDATOR.value
        try:
            result = await self._validate(selection, authority, candidate_text)
        except NarrationValidationError as exc:
            record_decision("validate", "error", {"error": str(exc)[:500]}, role=role)
            raise
        record_decision("validate", result.verdict, result.trace(candidate_text), role=role)
        return result

    async def _validate(
        self,
        selection: RoleModelSelection,
        authority: TurnAuthority,
        candidate_text: str,
    ) -> NarrationValidationResult:
        if not candidate_text.strip():
            raise NarrationValidationError("Narrator returned empty prose")
        messages = [
            ChatMessage(role="system", content=self.SYSTEM_PROMPT),
            ChatMessage(
                role="user",
                content=(
                    "[TURN FACTS]\n"
                    + json.dumps(authority.validator_payload(), ensure_ascii=False, indent=2)
                    + "\n\n[CANDIDATE NARRATION]\n"
                    + candidate_text
                ),
            ),
        ]
        try:
            data = await self._router.generate_json(
                self._provider,
                selection,
                messages,
                max_tokens=min(settings.NARRATION_VALIDATOR_MAX_TOKENS, 700),
                temperature=0.0,
                response_model=NarrationValidationResult,
            )
            return four_bans_only(NarrationValidationResult.model_validate(data))
        except (LLMProviderError, ValueError, TypeError) as exc:
            raise NarrationValidationError(str(exc)) from exc

    @staticmethod
    def repair_prompt(
        authority: TurnAuthority,
        candidate: str,
        result: NarrationValidationResult,
    ) -> str:
        violations = "\n".join(
            f"- {item.violation_type}: «{item.evidence}» -> {item.correction}"
            for item in result.violations
            if item.severity == "error"
        )
        return (
            "[REPAIR REJECTED NARRATION]\n"
            "Отредактируй текст минимально: исправь только перечисленные места и сохрани всё "
            "остальное, включая реплики присутствующих персонажей. Верни только художественную "
            "прозу на русском языке.\n\n"
            "ФАКТЫ ХОДА:\n"
            + json.dumps(authority.validator_payload(), ensure_ascii=False, indent=2)
            + "\n\nНАРУШЕНИЯ:\n"
            + (violations or result.summary)
            + "\n\n[REJECTED CANDIDATE]\n"
            + candidate
        )


__all__ = ["FOUR_BANS", "TurnAuthorityValidator", "four_bans_only"]
