from __future__ import annotations

import json

from app.config import settings
from app.models.narration_validation import NarrationValidationResult
from app.models.turn import ChatMessage
from app.models.turn_authority import TurnAuthority
from app.providers.llm_provider import LLMProvider, LLMProviderError
from app.services.narration_validator import NarrationValidationError
from app.services.role_model_router import RoleModelRouter, RoleModelSelection


class TurnAuthorityValidator:
    """Small control-model gate that judges prose against one typed authority object."""

    SYSTEM_PROMPT = """[TURN AUTHORITY VALIDATOR]
You are not a game master and you never continue the story. You receive one machine-readable
TURN AUTHORITY object and candidate prose. The authority object is the sole source of truth for
what this turn is allowed to establish.

Return repair_required only for concrete violations of that authority:
- PLAYER AGENCY: the prose adds voluntary protagonist dialogue, gestures, movement, choices,
  decisions, plans, beliefs, consent, promises, attacks or emotions that are not already contained
  in player_input. Merely describing perception or externally caused involuntary effects is allowed.
- CHARACTER PRESENCE: a known_absent_character acts/speaks/appears physically. Characters listed
  in present_characters are present. Characters listed in allowed_new_npcs are explicitly approved
  first-time introductions and MUST NOT be rejected as absent.
- UNPLANNED NPC: a genuinely new character not listed in allowed_new_npcs is introduced as present.
- MOVEMENT/TIME: prose completes a location/time/focus change not authorized by scene_disposition,
  transition_type or action_sequence.
- OUTCOME: prose contradicts approved observable_consequences or a completed structured sequence.
- COMPLICATION: prose invents a new threat/interruption/twist when allow_new_complication=false.

Do not reconstruct hidden campaign rules. Do not complain that an approved new NPC was not in the
old participant list. Do not invent corrections that change the approved turn outcome.

Return exactly:
{
  "verdict": "pass|repair_required",
  "summary": "short reason",
  "violations": [
    {
      "violation_type": "absent_character|absent_object|invalid_movement|invalid_time_advance|player_agency|ungrounded_complication|sequence_violation|canon_conflict|other",
      "severity": "warning|error",
      "evidence": "candidate fragment or precise description",
      "correction": "specific prose-only correction"
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
        if not candidate_text.strip():
            return NarrationValidationResult.model_validate(
                {
                    "verdict": "repair_required",
                    "summary": "Narrator produced empty prose.",
                    "violations": [
                        {
                            "violation_type": "other",
                            "severity": "error",
                            "evidence": "empty candidate",
                            "correction": "Produce the approved turn as narrative prose.",
                        }
                    ],
                }
            )
        messages = [
            ChatMessage(role="system", content=self.SYSTEM_PROMPT),
            ChatMessage(
                role="user",
                content=(
                    "[TURN AUTHORITY]\n"
                    + json.dumps(
                        authority.validator_payload(),
                        ensure_ascii=False,
                        indent=2,
                    )
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
            result = NarrationValidationResult.model_validate(data)
            return self.apply_deterministic_authority(result, authority)
        except (LLMProviderError, ValueError, TypeError) as exc:
            raise NarrationValidationError(str(exc)) from exc

    @staticmethod
    def apply_deterministic_authority(
        result: NarrationValidationResult,
        authority: TurnAuthority,
    ) -> NarrationValidationResult:
        """Never let a control-model hallucination overrule typed presence authority."""
        protected = {
            value.casefold()
            for value in [
                authority.acting_character_name,
                *authority.allowed_new_npc_names,
            ]
            if value
        }
        if not protected:
            return result

        filtered = []
        removed = False
        for violation in result.violations:
            if violation.violation_type != "absent_character":
                filtered.append(violation)
                continue
            text = f"{violation.evidence} {violation.correction}".casefold()
            if any(name in text for name in protected):
                removed = True
                continue
            filtered.append(violation)

        if not removed:
            return result
        errors = [item for item in filtered if item.severity == "error"]
        return NarrationValidationResult(
            verdict="repair_required" if errors else "pass",
            summary=(
                result.summary
                if errors
                else "Typed turn authority confirms the referenced speaker/introduction."
            ),
            violations=filtered,
        )

    @staticmethod
    def repair_prompt(
        authority: TurnAuthority,
        candidate: str,
        result: NarrationValidationResult,
    ) -> str:
        violations = "\n".join(
            f"- {item.violation_type}: {item.evidence} -> {item.correction}"
            for item in result.violations
            if item.severity == "error"
        )
        return (
            "[REPAIR AGAINST TURN AUTHORITY]\n"
            "Rewrite the candidate as one complete final Russian in-world response. Preserve only "
            "the outcomes authorized below. Remove every violation. Do not add a replacement NPC, "
            "twist, movement or protagonist action. Return prose only.\n\n"
            "AUTHORITY:\n"
            + json.dumps(authority.validator_payload(), ensure_ascii=False, indent=2)
            + "\n\nVIOLATIONS:\n"
            + (violations or result.summary)
            + "\n\nREJECTED CANDIDATE:\n"
            + candidate
        )


__all__ = ["TurnAuthorityValidator"]
