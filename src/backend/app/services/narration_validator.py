from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.narration_validation_table import NarrationValidationRun
from app.models.narration_validation import NarrationValidationResult
from app.models.turn import ChatMessage
from app.providers.llm_provider import LLMProvider, LLMProviderError
from app.services.role_model_router import RoleModelRouter, RoleModelSelection


class NarrationValidationError(RuntimeError):
    """Raised when the validator itself cannot produce a trustworthy verdict."""


@dataclass(frozen=True)
class NarrationGateResult:
    status: str
    final_text: str
    validation_run_id: UUID
    attempts: list[dict]
    repair_attempts: int
    violation_count: int
    failure_reason: str | None = None


class NarrationValidator:
    """Validate prose against the already-authoritative turn context."""

    SYSTEM_PROMPT = """[POST-GENERATION NARRATION VALIDATOR]
You are a strict continuity and player-agency validator. You do not continue the story and you do
not rewrite prose. Compare the candidate narration with the supplied authoritative campaign
context, approved plan, executed action sequence, and scene bridge.

Return repair_required when the candidate does any of the following:
- lets a character speak, act, observe, touch, or appear despite being absent from the exhaustive
  physical participant list or explicitly left behind by the scene bridge;
- uses an object that is not physically present or owned according to structured context;
- moves through an exit, route, vehicle, portal, or destination that was not structurally approved;
- advances world time beyond the approved transition;
- writes unprovided protagonist dialogue, decisions, plans, beliefs, consent, trust, attraction,
  fear, promises, refusals, attacks, voluntary actions, or emotional conclusions;
- invents a threat, interruption, secret, visitor, refusal, hidden price, accident, countdown, or
  ominous beat when narration_policy disallows a new complication;
- narrates a blocked or skipped action-sequence step as completed;
- contradicts explicit canon or the structured result of the turn.

Do not flag:
- sensory perception available to the protagonist;
- externally caused involuntary physical effects;
- the mere mention of an absent character inside an explicit negative placement statement;
- stylistic preferences that do not violate continuity, agency, or the approved resolution.

Every error must quote or precisely identify evidence from the candidate and state a concrete
correction. Return exactly this JSON schema:
{
  "verdict": "pass|repair_required",
  "summary": "short reason",
  "violations": [
    {
      "violation_type": "absent_character|absent_object|invalid_movement|invalid_time_advance|player_agency|ungrounded_complication|sequence_violation|canon_conflict|other",
      "severity": "warning|error",
      "evidence": "candidate fragment or exact description",
      "correction": "specific repair instruction"
    }
  ]
}
"""

    REPAIR_PROMPT = """[REPAIR REJECTED NARRATION]
Rewrite the rejected candidate into one complete final in-world response.

Hard requirements:
- Preserve the approved outcome and every completed action-sequence step.
- Remove every listed violation; do not explain the repair process.
- Do not add a replacement twist, threat, NPC, object, movement, time jump, or player decision.
- Keep the protagonist's dialogue, choices, plans, beliefs, consent, and emotional conclusions open
  unless the player explicitly supplied them.
- Return only the repaired narrative prose, with no headings, JSON, notes, or apology.

Violations:
{violations}

Rejected candidate:
{candidate}
"""

    def __init__(self, session: AsyncSession, router: RoleModelRouter):
        self._session = session
        self._router = router
        self._provider = LLMProvider()

    @property
    def telemetry(self) -> dict:
        return dict(self._provider.last_telemetry or {})

    async def start_run(
        self,
        campaign_id: UUID,
        trigger_turn_id: UUID,
        scene_id: UUID | None,
        draft_text: str,
        validator_model_name: str | None,
    ) -> NarrationValidationRun:
        row = NarrationValidationRun(
            campaign_id=str(campaign_id),
            trigger_turn_id=str(trigger_turn_id),
            scene_id=str(scene_id) if scene_id else None,
            status="validating",
            draft_text=draft_text,
            attempts_json="[]",
            validator_model_name=validator_model_name,
        )
        self._session.add(row)
        await self._session.commit()
        return row

    async def record_attempt(
        self,
        row: NarrationValidationRun,
        *,
        attempt_index: int,
        candidate_text: str,
        result: NarrationValidationResult,
        telemetry: dict,
    ) -> None:
        attempts = json.loads(row.attempts_json or "[]")
        attempts.append(
            {
                "attempt_index": attempt_index,
                "candidate_text": candidate_text,
                "verdict": result.verdict,
                "summary": result.summary,
                "violations": [item.model_dump() for item in result.violations],
                "telemetry": telemetry,
            }
        )
        row.attempts_json = json.dumps(attempts, ensure_ascii=False)
        row.violation_count = sum(
            1
            for attempt in attempts
            for item in attempt.get("violations", [])
            if item.get("severity") == "error"
        )
        await self._session.commit()

    async def finalize(
        self,
        row: NarrationValidationRun,
        *,
        status: str,
        final_text: str | None,
        repair_attempts: int,
        failure_reason: str | None = None,
        assistant_turn_id: UUID | None = None,
    ) -> NarrationGateResult:
        row.status = status
        row.final_text = final_text
        row.repair_attempts = repair_attempts
        row.failure_reason = failure_reason
        row.assistant_turn_id = str(assistant_turn_id) if assistant_turn_id else None
        await self._session.commit()
        attempts = json.loads(row.attempts_json or "[]")
        return NarrationGateResult(
            status=status,
            final_text=final_text or "",
            validation_run_id=UUID(row.id),
            attempts=attempts,
            repair_attempts=repair_attempts,
            violation_count=row.violation_count,
            failure_reason=failure_reason,
        )

    @staticmethod
    def protect_confirmed_speaker(
        result: NarrationValidationResult,
        confirmed_speaker_name: str | None,
    ) -> NarrationValidationResult:
        """Do not let a control-model hallucination overrule deterministic presence."""
        name = (confirmed_speaker_name or "").strip()
        if not name:
            return result
        needle = name.casefold()
        filtered = []
        removed = False
        for violation in result.violations:
            text = f"{violation.evidence} {violation.correction}".casefold()
            if violation.violation_type == "absent_character" and needle in text:
                removed = True
                continue
            filtered.append(violation)
        if not removed:
            return result
        has_errors = any(item.severity == "error" for item in filtered)
        return NarrationValidationResult(
            verdict="repair_required" if has_errors else "pass",
            summary=(
                result.summary
                if has_errors
                else f"Deterministic scene state confirms {name} as the active speaker."
            ),
            violations=filtered,
        )

    async def validate(
        self,
        selection: RoleModelSelection,
        context_messages: list[ChatMessage],
        candidate_text: str,
        *,
        confirmed_speaker_name: str | None = None,
    ) -> NarrationValidationResult:
        if not candidate_text.strip():
            return NarrationValidationResult(
                verdict="repair_required",
                summary="Narrator produced empty prose.",
                violations=[
                    {
                        "violation_type": "other",
                        "severity": "error",
                        "evidence": "empty candidate",
                        "correction": "Produce a complete in-world response.",
                    }
                ],
            )
        if not context_messages:
            raise NarrationValidationError("validator received empty context")

        first, *rest = context_messages
        speaker_contract = ""
        if confirmed_speaker_name:
            speaker_contract = (
                "\n\n[DETERMINISTIC ACTIVE SPEAKER]\n"
                f"{confirmed_speaker_name} is confirmed by the engine to be physically "
                "present in the current scene and is the explicitly selected speaker. "
                "Do not report this character as absent. This deterministic fact "
                "overrides model inference."
            )
        messages = [
            ChatMessage(
                role="system",
                content=(
                    f"{self.SYSTEM_PROMPT}{speaker_contract}\n\n"
                    f"[AUTHORITATIVE TURN CONTEXT]\n{first.content}"
                ),
            ),
            *rest,
            ChatMessage(
                role="user",
                content=f"[CANDIDATE NARRATION]\n{candidate_text}",
            ),
        ]
        try:
            data = await self._router.generate_json(
                self._provider,
                selection,
                messages,
                max_tokens=settings.NARRATION_VALIDATOR_MAX_TOKENS,
                temperature=settings.NARRATION_VALIDATOR_TEMPERATURE,
                response_model=NarrationValidationResult,
            )
            result = NarrationValidationResult.model_validate(data)
            return self.protect_confirmed_speaker(result, confirmed_speaker_name)
        except (LLMProviderError, ValueError, TypeError) as exc:
            raise NarrationValidationError(str(exc)) from exc

    @classmethod
    def repair_messages(
        cls,
        context_messages: list[ChatMessage],
        candidate_text: str,
        validation: NarrationValidationResult,
    ) -> list[ChatMessage]:
        violations = "\n".join(
            f"- {item.violation_type}: {item.evidence} -> {item.correction}"
            for item in validation.violations
            if item.severity == "error"
        )
        return [
            *context_messages,
            ChatMessage(
                role="user",
                content=cls.REPAIR_PROMPT.format(
                    violations=violations or validation.summary,
                    candidate=candidate_text,
                ),
            ),
        ]
