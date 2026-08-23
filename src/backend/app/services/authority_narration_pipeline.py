from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.repositories.provider_config_repo import ProviderConfigRepository
from app.models.narration_validation import NarrationValidationResult
from app.models.turn import ChatMessage
from app.models.turn_authority import TurnAuthority
from app.providers.llm_provider import (
    LLMProvider,
    LLMProviderError,
    LLMProviderTruncatedError,
)
from app.services.narration_publication_guard import NarrationPublicationGuard
from app.services.narration_repetition_guard import NarrationRepetitionGuard
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
    """Render one authoritative turn without letting renderer mistakes cancel game state.

    Planner/engine own the outcome. Narrator and validator are presentation layers. A semantic prose
    violation first gets a deterministic surgical removal when exact evidence permits it, then one
    preserve-first model repair if necessary, and finally a deterministic safe publication. Only
    real provider or structured-state failures are allowed to fail the turn.
    """

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

    @staticmethod
    def _merge_continuation(prefix: str, continuation: str) -> str:
        if not prefix:
            return continuation
        if not continuation:
            return prefix
        max_overlap = min(300, len(prefix), len(continuation))
        for size in range(max_overlap, 15, -1):
            if prefix[-size:].casefold() == continuation[:size].casefold():
                return prefix + continuation[size:]
        separator = (
            ""
            if prefix.endswith((" ", "\n"))
            or continuation.startswith((" ", "\n"))
            else " "
        )
        return prefix + separator + continuation

    @staticmethod
    def _continuation_messages(
        messages: list[ChatMessage],
        partial_text: str,
    ) -> list[ChatMessage]:
        return [
            *messages,
            ChatMessage(role="assistant", content=partial_text[-4000:]),
            ChatMessage(
                role="user",
                content=(
                    "[CONTINUE TRUNCATED NARRATION]\n"
                    "Продолжи ровно с места обрыва. Не повторяй уже написанное, не меняй исход "
                    "хода и не добавляй новых действий героя. Дай только завершение художественного "
                    "ответа на русском языке."
                ),
            ),
        ]

    async def _stream_once(
        self,
        messages: list[ChatMessage],
        selection: RoleModelSelection,
        *,
        temperature: float,
    ) -> tuple[str, dict]:
        chunks: list[str] = []
        async for token in self._provider.generate_stream(
            messages,
            selection.config,
            selection.api_key,
            temperature=temperature,
        ):
            chunks.append(token)
        text = "".join(chunks).strip()
        if not text:
            raise LLMProviderError("Narrator returned empty prose")
        return text, dict(self._provider.last_telemetry or {})

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
            first_telemetry = dict(self._provider.last_telemetry or {})
            partial = "".join(chunks).strip() or exc.partial_text.strip()
            finish_reason = str(first_telemetry.get("finish_reason") or "").casefold()
            if partial and finish_reason == "stop":
                first_telemetry["completion_recovered_from_false_punctuation_truncation"] = True
                first_telemetry["status"] = "completed"
                return partial, first_telemetry
            if not partial:
                raise

            continuation_messages = self._continuation_messages(messages, partial)
            continuation, second_telemetry = await self._stream_once(
                continuation_messages,
                selection,
                temperature=temperature,
            )
            merged = self._merge_continuation(partial, continuation).strip()
            if not merged:
                raise LLMProviderError("Narrator truncation recovery produced no usable prose")
            return merged, {
                **second_telemetry,
                "truncation_recovery": {
                    "status": "continued",
                    "first_attempt": first_telemetry,
                    "partial_characters": len(partial),
                    "continuation_characters": len(continuation),
                },
            }

        text = "".join(chunks).strip()
        if not text:
            raise LLMProviderError("Narrator returned empty prose")
        return text, dict(self._provider.last_telemetry or {})

    async def _generate_non_repeating(
        self,
        *,
        campaign_id: UUID,
        scene_id: UUID | None,
        authority: TurnAuthority,
        messages: list[ChatMessage],
        selection: RoleModelSelection,
        temperature: float,
    ) -> tuple[str, dict, bool]:
        guard = NarrationRepetitionGuard(self._session)
        previous = await guard.recent_responses(campaign_id, scene_id, authority)
        candidate, first_telemetry = await self._generate_text(
            messages,
            selection,
            temperature=temperature,
        )
        actor_turn = authority.scene_disposition == "actor_turn"
        first_match = guard.detect(candidate, previous, actor_turn=actor_turn)
        if first_match is None:
            return candidate, first_telemetry, False

        retry, retry_telemetry = await self._generate_text(
            guard.retry_messages(messages, authority, first_match),
            selection,
            temperature=min(0.7, max(temperature, 0.35)),
        )
        second_match = guard.detect(retry, previous, actor_turn=actor_turn)
        return retry, {
            **retry_telemetry,
            "repetition_guard": {
                "detected": True,
                "first_similarity": round(first_match.similarity, 4),
                "first_exact": first_match.exact,
                "retried": True,
                "retry_similarity": (
                    round(second_match.similarity, 4) if second_match else None
                ),
                "exhausted": second_match is not None,
                "first_generation": first_telemetry,
            },
        }, second_match is not None

    @staticmethod
    def _synthetic_safe_result(summary: str) -> NarrationValidationResult:
        return NarrationValidationResult(
            verdict="pass",
            summary=summary,
            violations=[],
        )

    async def _publish_fallback(
        self,
        *,
        audit: NarrationValidator,
        run,
        authority: TurnAuthority,
        candidate: str,
        validation: NarrationValidationResult | None,
        repair_attempts: int,
        attempt_index: int,
        reason: str,
        telemetry: dict,
    ) -> AuthorityNarrationResult:
        published, publication = NarrationPublicationGuard.publish(
            authority,
            candidate,
            validation,
        )
        await audit.record_attempt(
            run,
            attempt_index=attempt_index,
            candidate_text=published,
            result=self._synthetic_safe_result(
                "Deterministic publication guard rendered the already-authoritative turn."
            ),
            telemetry={
                "authority_version": authority.version,
                "publication_guard": publication,
                "reason": reason,
            },
        )
        gate = await audit.finalize(
            run,
            status="repaired",
            final_text=published,
            repair_attempts=repair_attempts,
            failure_reason=reason[:2000],
        )
        return AuthorityNarrationResult(
            text=published,
            telemetry={
                **telemetry,
                "narration_validation": {
                    "status": gate.status,
                    "validation_run_id": str(gate.validation_run_id),
                    "authority_version": authority.version,
                    "publication_guard": publication,
                    "semantic_failure_recovered": True,
                    "reason": reason[:2000],
                },
            },
            validation_run_id=gate.validation_run_id,
            validation_status="safe_fallback",
        )

    async def _try_surgical_repair(
        self,
        *,
        audit: NarrationValidator,
        run,
        validator: TurnAuthorityValidator,
        validation_selection: RoleModelSelection,
        authority: TurnAuthority,
        draft: str,
        initial_result: NarrationValidationResult,
        attempt_index: int,
    ) -> tuple[str | None, NarrationValidationResult | None, dict, bool]:
        candidate, surgery = NarrationPublicationGuard.surgical_repair_candidate(
            draft,
            initial_result,
        )
        if candidate is None:
            return None, None, surgery, False

        result = await validator.validate(
            validation_selection,
            authority,
            candidate,
        )
        await audit.record_attempt(
            run,
            attempt_index=attempt_index,
            candidate_text=candidate,
            result=result,
            telemetry={
                **validator.telemetry,
                "authority_version": authority.version,
                "repair_strategy": "deterministic_span_removal",
                "surgical_repair": surgery,
            },
        )
        return (
            candidate if result.verdict == "pass" else None,
            result,
            surgery,
            True,
        )

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
        draft, narrator_telemetry, repetition_exhausted = await self._generate_non_repeating(
            campaign_id=campaign_id,
            scene_id=scene_id,
            authority=authority,
            messages=narrator_messages,
            selection=narrator_selection,
            temperature=settings.NARRATOR_TEMPERATURE,
        )

        validation_selection = await self._router.resolve(
            campaign_id,
            ModelRole.NARRATION_VALIDATOR,
            narrator_selection.config,
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
            validation_selection.config.model_name if validation_selection else None,
        )

        if repetition_exhausted:
            return await self._publish_fallback(
                audit=audit,
                run=run,
                authority=authority,
                candidate="",
                validation=None,
                repair_attempts=1,
                attempt_index=0,
                reason="near-verbatim narration repetition persisted after one regeneration",
                telemetry=narrator_telemetry,
            )

        if validation_selection is None:
            if settings.NARRATION_VALIDATOR_FAIL_OPEN:
                published, publication = NarrationPublicationGuard.publish(
                    authority,
                    draft,
                    None,
                )
                gate = await audit.finalize(
                    run,
                    status="failed_open",
                    final_text=published,
                    repair_attempts=0,
                    failure_reason="validator routing unavailable",
                )
                return AuthorityNarrationResult(
                    text=published,
                    telemetry={
                        **narrator_telemetry,
                        "narration_validation": {
                            "status": gate.status,
                            "reason": "validator routing unavailable",
                            "authority_version": authority.version,
                            "publication_guard": publication,
                        },
                    },
                    validation_run_id=gate.validation_run_id,
                    validation_status="failed_open",
                )
            return await self._publish_fallback(
                audit=audit,
                run=run,
                authority=authority,
                candidate=draft,
                validation=None,
                repair_attempts=0,
                attempt_index=0,
                reason="validator routing unavailable",
                telemetry=narrator_telemetry,
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

            surgical, surgical_result, surgery, surgery_attempted = await self._try_surgical_repair(
                audit=audit,
                run=run,
                validator=validator,
                validation_selection=validation_selection,
                authority=authority,
                draft=draft,
                initial_result=result,
                attempt_index=1,
            )
            if surgical is not None:
                gate = await audit.finalize(
                    run,
                    status="repaired",
                    final_text=surgical,
                    repair_attempts=1,
                )
                return AuthorityNarrationResult(
                    text=surgical,
                    telemetry={
                        **narrator_telemetry,
                        "narration_validation": {
                            "status": gate.status,
                            "validation_run_id": str(gate.validation_run_id),
                            "authority_version": authority.version,
                            "repair_strategy": "deterministic_span_removal",
                            "surgical_repair": surgery,
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
            repaired, repair_telemetry, repair_repetition_exhausted = (
                await self._generate_non_repeating(
                    campaign_id=campaign_id,
                    scene_id=scene_id,
                    authority=authority,
                    messages=repair_messages,
                    selection=narrator_selection,
                    temperature=settings.NARRATION_REPAIR_TEMPERATURE,
                )
            )
            model_attempt_index = 2 if surgery_attempted else 1
            repair_attempts = 2 if surgery_attempted else 1
            if repair_repetition_exhausted:
                return await self._publish_fallback(
                    audit=audit,
                    run=run,
                    authority=authority,
                    candidate="",
                    validation=None,
                    repair_attempts=repair_attempts,
                    attempt_index=model_attempt_index,
                    reason="repaired narration repeated a prior published response after retry",
                    telemetry={
                        **narrator_telemetry,
                        "surgical_repair": surgery,
                        "repair_generation": repair_telemetry,
                    },
                )

            repaired_result = await validator.validate(
                validation_selection,
                authority,
                repaired,
            )
            await audit.record_attempt(
                run,
                attempt_index=model_attempt_index,
                candidate_text=repaired,
                result=repaired_result,
                telemetry={
                    **validator.telemetry,
                    "authority_version": authority.version,
                    "repair_strategy": "preserve_first_model_edit",
                    "surgical_repair": surgery,
                    "repair_generation": repair_telemetry,
                },
            )
            if repaired_result.verdict != "pass":
                return await self._publish_fallback(
                    audit=audit,
                    run=run,
                    authority=authority,
                    candidate=repaired,
                    validation=repaired_result,
                    repair_attempts=repair_attempts,
                    attempt_index=model_attempt_index + 1,
                    reason=(
                        repaired_result.summary
                        or "narration remained outside turn authority after repair"
                    ),
                    telemetry={
                        **narrator_telemetry,
                        "surgical_repair": surgery,
                        "repair_generation": repair_telemetry,
                        "validator_telemetry": validator.telemetry,
                    },
                )

            gate = await audit.finalize(
                run,
                status="repaired",
                final_text=repaired,
                repair_attempts=repair_attempts,
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
                        "repair_strategy": "preserve_first_model_edit",
                        "surgical_repair": surgery,
                        "validator_telemetry": validator.telemetry,
                    },
                },
                validation_run_id=gate.validation_run_id,
                validation_status=gate.status,
            )
        except NarrationValidationError as exc:
            if settings.NARRATION_VALIDATOR_FAIL_OPEN:
                published, publication = NarrationPublicationGuard.publish(
                    authority,
                    draft,
                    None,
                )
                gate = await audit.finalize(
                    run,
                    status="failed_open",
                    final_text=published,
                    repair_attempts=0,
                    failure_reason=str(exc)[:2000],
                )
                return AuthorityNarrationResult(
                    text=published,
                    telemetry={
                        **narrator_telemetry,
                        "narration_validation": {
                            "status": gate.status,
                            "validation_run_id": str(gate.validation_run_id),
                            "reason": str(exc)[:2000],
                            "authority_version": authority.version,
                            "publication_guard": publication,
                        },
                    },
                    validation_run_id=gate.validation_run_id,
                    validation_status=gate.status,
                )
            return await self._publish_fallback(
                audit=audit,
                run=run,
                authority=authority,
                candidate=draft,
                validation=None,
                repair_attempts=0,
                attempt_index=0,
                reason=f"authority validator failed: {exc}",
                telemetry=narrator_telemetry,
            )


__all__ = ["AuthorityNarrationPipeline", "AuthorityNarrationResult"]
