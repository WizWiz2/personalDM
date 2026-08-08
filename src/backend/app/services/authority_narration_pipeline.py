from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.repositories.provider_config_repo import ProviderConfigRepository
from app.models.turn import ChatMessage
from app.models.turn_authority import TurnAuthority
from app.providers.llm_provider import (
    LLMProvider,
    LLMProviderError,
    LLMProviderTruncatedError,
)
from app.services.narration_validator import NarrationValidationError, NarrationValidator
from app.services.role_model_router import ModelRole, RoleModelRouter, RoleModelSelection
from app.services.turn_authority_validator import TurnAuthorityValidator


@dataclass(frozen=True)
class AuthorityNarrationResult:
    text: str
    telemetry: dict
    validation_run_id: UUID | None = None
    validation_status: str = "not_invoked"


class AuthorityNarrationPipeline:
    """Render one approved turn and validate prose against the exact same authority."""

    def __init__(
        self,
        session: AsyncSession,
        router: RoleModelRouter,
        provider: LLMProvider | None = None,
    ):
        self._session = session
        self._router = router
        self._provider = provider or LLMProvider()

    @property
    def last_telemetry(self) -> dict:
        return dict(self._provider.last_telemetry or {})

    async def _generate_text(
        self,
        messages: list[ChatMessage],
        selection: RoleModelSelection,
        *,
        temperature: float,
    ) -> tuple[str, dict]:
        chunks: list[str] = []
        try:
            async for token in self._provider.generate_stream(
                messages,
                selection.config,
                selection.api_key,
                temperature=temperature,
            ):
                chunks.append(token)
        except LLMProviderTruncatedError as exc:
            telemetry = dict(self._provider.last_telemetry or {})
            text = "".join(chunks).strip() or exc.partial_text.strip()
            # Ollama/native providers explicitly saying `stop` is stronger evidence than
            # our old punctuation heuristic. A prose response may legitimately end in a
            # name, number, em dash, markdown emphasis or other non-listed character.
            if text and str(telemetry.get("finish_reason") or "").casefold() == "stop":
                telemetry["completion_recovered_from_false_punctuation_truncation"] = True
                telemetry["status"] = "completed"
                return text, telemetry
            raise
        text = "".join(chunks).strip()
        if not text:
            raise LLMProviderError("Narrator returned empty prose")
        return text, dict(self._provider.last_telemetry or {})

    async def generate(
        self,
        *,
        campaign_id: UUID,
        trigger_turn_id: UUID,
        scene_id: UUID | None,
        narrator_messages: list[ChatMessage],
        narrator_selection: RoleModelSelection,
        authority: TurnAuthority,
    ) -> AuthorityNarrationResult:
        draft, narrator_telemetry = await self._generate_text(
            narrator_messages,
            narrator_selection,
            temperature=settings.NARRATOR_TEMPERATURE,
        )

        validation_selection = await self._router.resolve(
            campaign_id,
            ModelRole.NARRATION_VALIDATOR,
            narrator_selection.config,
        )
        if validation_selection is None:
            if not settings.NARRATION_VALIDATOR_FAIL_OPEN:
                raise LLMProviderError("narration validator model routing is unavailable")
            return AuthorityNarrationResult(
                text=draft,
                telemetry={
                    **narrator_telemetry,
                    "narration_validation": {
                        "status": "failed_open",
                        "reason": "validator routing unavailable",
                        "authority_version": authority.version,
                    },
                },
                validation_status="failed_open",
            )

        audit = NarrationValidator(
            self._session,
            RoleModelRouter(ProviderConfigRepository(self._session)),
        )
        run = await audit.start_run(
            campaign_id,
            trigger_turn_id,
            scene_id,
            draft,
            validation_selection.config.model_name,
        )
        validator = TurnAuthorityValidator(self._router)
        try:
            result = await validator.validate(validation_selection, authority, draft)
            await audit.record_attempt(
                run,
                attempt_index=0,
                candidate_text=draft,
                result=result,
                telemetry={
                    **validator.telemetry,
                    "authority_version": authority.version,
                },
            )
            if result.verdict == "pass":
                gate = await audit.finalize(
                    run,
                    status="passed",
                    final_text=draft,
                    repair_attempts=0,
                )
                return AuthorityNarrationResult(
                    text=draft,
                    telemetry={
                        **narrator_telemetry,
                        "narration_validation": {
                            "status": gate.status,
                            "validation_run_id": str(gate.validation_run_id),
                            "authority_version": authority.version,
                            "validator_telemetry": validator.telemetry,
                        },
                    },
                    validation_run_id=gate.validation_run_id,
                    validation_status=gate.status,
                )

            repair_messages = [
                *narrator_messages,
                ChatMessage(
                    role="user",
                    content=validator.repair_prompt(authority, draft, result),
                ),
            ]
            repaired, repair_telemetry = await self._generate_text(
                repair_messages,
                narrator_selection,
                temperature=settings.NARRATION_REPAIR_TEMPERATURE,
            )
            repaired_result = await validator.validate(
                validation_selection,
                authority,
                repaired,
            )
            await audit.record_attempt(
                run,
                attempt_index=1,
                candidate_text=repaired,
                result=repaired_result,
                telemetry={
                    **validator.telemetry,
                    "authority_version": authority.version,
                    "repair_generation": repair_telemetry,
                },
            )
            if repaired_result.verdict != "pass":
                gate = await audit.finalize(
                    run,
                    status="rejected",
                    final_text=None,
                    repair_attempts=1,
                    failure_reason=(
                        repaired_result.summary or "narration remained outside turn authority"
                    ),
                )
                raise LLMProviderError(
                    "Narration rejected after authority repair: "
                    f"{gate.failure_reason or 'authority violation'}"
                )

            gate = await audit.finalize(
                run,
                status="repaired",
                final_text=repaired,
                repair_attempts=1,
            )
            return AuthorityNarrationResult(
                text=repaired,
                telemetry={
                    **narrator_telemetry,
                    "repair_generation": repair_telemetry,
                    "narration_validation": {
                        "status": gate.status,
                        "validation_run_id": str(gate.validation_run_id),
                        "authority_version": authority.version,
                        "validator_telemetry": validator.telemetry,
                    },
                },
                validation_run_id=gate.validation_run_id,
                validation_status=gate.status,
            )
        except NarrationValidationError as exc:
            status = "failed_open" if settings.NARRATION_VALIDATOR_FAIL_OPEN else "rejected"
            gate = await audit.finalize(
                run,
                status=status,
                final_text=draft if settings.NARRATION_VALIDATOR_FAIL_OPEN else None,
                repair_attempts=0,
                failure_reason=str(exc)[:2000],
            )
            if not settings.NARRATION_VALIDATOR_FAIL_OPEN:
                raise LLMProviderError(f"Narration authority validator failed: {exc}") from exc
            return AuthorityNarrationResult(
                text=draft,
                telemetry={
                    **narrator_telemetry,
                    "narration_validation": {
                        "status": gate.status,
                        "validation_run_id": str(gate.validation_run_id),
                        "reason": str(exc)[:2000],
                        "authority_version": authority.version,
                    },
                },
                validation_run_id=gate.validation_run_id,
                validation_status=gate.status,
            )


__all__ = ["AuthorityNarrationPipeline", "AuthorityNarrationResult"]
