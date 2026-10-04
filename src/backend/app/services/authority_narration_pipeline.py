from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from pydantic import create_model
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.tables import Belief, Entity
from app.db.repositories.provider_config_repo import ProviderConfigRepository
from app.models.narration_validation import GrantedBeat, GrantedNarration, NarrationValidationResult
from app.models.turn import ChatMessage
from app.models.turn_authority import TurnAuthority
from app.providers.llm_provider import (
    LLMProvider,
    LLMProviderError,
    LLMProviderTruncatedError,
)
from app.services.actor_turn_authority_guard import _word_key, segment_actor_response
from app.services.narration_publication_guard import NarrationPublicationGuard
from app.services.narration_validator import NarrationValidationError, NarrationValidator
from app.services.role_model_router import ModelRole, RoleModelRouter, RoleModelSelection
from app.services.llm_usage_tracker import record_decision, record_provider_telemetry
from app.services.turn_authority_validator import TurnAuthorityValidator


@dataclass(frozen=True)
class AuthorityNarrationResult:
    text: str
    telemetry: dict
    validation_run_id: UUID | None = None
    validation_status: str = "not_invoked"


class AuthorityNarrationPipeline:
    """Render one authoritative turn without letting renderer mistakes cancel game state.

    Planner/engine own the outcome. Code checks the typed beat grant, one validator call judges the
    four bans; a rejected draft gets one model repair; then the typed step outcome is published.
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

    async def _record_narrator_usage(
        self,
        selection: RoleModelSelection,
        telemetry: dict,
    ) -> dict:
        annotated = {
            **dict(telemetry or {}),
            "model_role": ModelRole.NARRATOR.value,
            "role_model_source": selection.source,
            "resolved_model": selection.config.model_name,
        }
        await record_provider_telemetry(annotated)
        return annotated

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
        telemetry = await self._record_narrator_usage(
            selection,
            dict(self._provider.last_telemetry or {}),
        )
        return text, telemetry

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
            first_telemetry = await self._record_narrator_usage(
                selection,
                dict(self._provider.last_telemetry or {}),
            )
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
        except LLMProviderError:
            await self._record_narrator_usage(
                selection,
                dict(self._provider.last_telemetry or {}),
            )
            raise

        text = "".join(chunks).strip()
        if not text:
            raise LLMProviderError("Narrator returned empty prose")
        telemetry = await self._record_narrator_usage(
            selection,
            dict(self._provider.last_telemetry or {}),
        )
        return text, telemetry

    async def _narrate(
        self,
        messages: list[ChatMessage],
        selection: RoleModelSelection,
        authority: TurnAuthority,
        *,
        temperature: float,
    ) -> tuple[str, GrantedBeat | None, dict]:
        """Prose; under a beat grant, structured prose plus the narrator's typed beat claim."""
        if authority.beat_owner_id is None:
            text, telemetry = await self._generate_text(messages, selection, temperature=temperature)
            return text, None, telemetry
        # The schema admits only the owner's ID: strict decoding cannot mistype it (live B5 T4).
        owner = create_model("GrantedBeat", __base__=GrantedBeat,
                             cast_id=(Literal[str(authority.beat_owner_id)], ...))
        wire = create_model("GrantedNarration", __base__=GrantedNarration, beat=(owner, ...))
        try:
            narration = wire.model_validate(await self._router.generate_json(
                self._provider, selection, messages,
                temperature=temperature, response_model=wire,
            ))
        except (ValueError, TypeError) as exc:
            raise LLMProviderError(f"granted narration is malformed: {exc}") from exc
        return narration.prose.strip(), narration.beat, self.last_telemetry

    @staticmethod
    def _beat_failure(authority: TurnAuthority, prose: str, beat: GrantedBeat | None) -> str | None:
        if authority.beat_owner_id is None:
            return None
        failure = beat.failure(authority.beat_owner_id, authority.beat_owner_name, prose) if beat else "no beat returned"
        record_decision("beat", "unhonored" if failure else "honored", {
            "owner": authority.beat_owner_name, "owner_id": str(authority.beat_owner_id),
            **(beat.model_dump() if beat else {}), "failure": failure,
        })
        return failure

    async def _repeats(self, campaign_id: UUID, prose: str) -> list[str]:
        """Sentences equal (up to case/punctuation) to a line an NPC already said: its scribe ledger."""
        said = {_word_key(line) for line in (await self._session.execute(
            select(Belief.proposition).join(Entity, Entity.id == Belief.source_character_id)
            .where(Entity.campaign_id == str(campaign_id), Belief.is_current.is_(True))
        )).scalars()}
        repeats = [line for line in segment_actor_response(prose, max_segments=80)
                   if _word_key(line) in said]
        if repeats:
            record_decision("repeat", "rejected", {"sentences": repeats})
        return repeats

    async def _check(
        self,
        *,
        validator: TurnAuthorityValidator,
        audit: NarrationValidator,
        run,
        selection: RoleModelSelection,
        authority: TurnAuthority,
        candidate: str,
        attempt_index: int,
    ) -> NarrationValidationResult:
        result = await validator.validate(selection, authority, candidate)
        await audit.record_attempt(
            run,
            attempt_index=attempt_index,
            candidate_text=candidate,
            result=result,
            telemetry={**validator.telemetry, "authority_version": authority.version},
        )
        return result

    async def _finish(
        self,
        *,
        audit: NarrationValidator,
        run,
        authority: TurnAuthority,
        text: str,
        status: str,
        repair_attempts: int,
        telemetry: dict,
        publication: dict,
        reason: str | None = None,
    ) -> AuthorityNarrationResult:
        gate = await audit.finalize(
            run,
            status="repaired" if status == "safe_fallback" else status,
            final_text=text,
            repair_attempts=repair_attempts,
            failure_reason=reason[:2000] if reason else None,
        )
        return AuthorityNarrationResult(
            text=text,
            telemetry={
                **telemetry,
                "narration_validation": {
                    "status": gate.status,
                    "validation_run_id": str(gate.validation_run_id),
                    "authority_version": authority.version,
                    "publication_guard": publication,
                    **({"reason": reason[:2000]} if reason else {}),
                },
            },
            validation_run_id=gate.validation_run_id,
            validation_status=status,
        )

    async def _fallback(
        self,
        *,
        audit: NarrationValidator,
        run,
        authority: TurnAuthority,
        reason: str,
        repair_attempts: int,
        telemetry: dict,
        status: str = "safe_fallback",
    ) -> AuthorityNarrationResult:
        """Publish the typed step outcome as plain text; raises only if there is none."""
        record_decision("publish", "authority_projection", {"reason": reason[:500]})
        text, publication = NarrationPublicationGuard.publish(authority, "", None)
        return await self._finish(
            audit=audit,
            run=run,
            authority=authority,
            text=text,
            status=status,
            repair_attempts=repair_attempts,
            telemetry=telemetry,
            publication=publication,
            reason=reason,
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
        """Draft, one validator call, at most one repair, then the typed-outcome fallback."""
        audit = NarrationValidator(
            self._session,
            RoleModelRouter(ProviderConfigRepository(self._session)),
        )
        try:
            draft, beat, telemetry = await self._narrate(
                narrator_messages,
                narrator_selection,
                authority,
                temperature=settings.NARRATOR_TEMPERATURE,
            )
        except LLMProviderError as exc:
            run = await audit.start_run(campaign_id, trigger_turn_id, scene_id, "", None)
            return await self._fallback(
                audit=audit,
                run=run,
                authority=authority,
                reason=f"narrator failed: {type(exc).__name__}: {exc}",
                repair_attempts=0,
                telemetry={**self.last_telemetry, "narration_degraded": True},
            )
        validation_selection = await self._router.resolve(
            campaign_id,
            ModelRole.NARRATION_VALIDATOR,
            narrator_selection.config,
        )
        validator_model = validation_selection.config.model_name if validation_selection else None
        run = await audit.start_run(campaign_id, trigger_turn_id, scene_id, draft, validator_model)

        def accepted(text: str, status: str, attempts: int, reason: str | None = None):
            record_decision("publish", "validated_candidate", {"status": status})
            return self._finish(
                audit=audit,
                run=run,
                authority=authority,
                text=text.strip(),
                status=status,
                repair_attempts=attempts,
                telemetry=telemetry,
                publication={"mode": "validated_candidate", "validated_surface": True},
                reason=reason,
            )

        async def validator_down(reason: str):
            # An unchecked draft stays off the surface while a typed outcome exists.
            # Fail-open publishes the draft only when there is nothing typed to show.
            if settings.NARRATION_VALIDATOR_FAIL_OPEN and not NarrationPublicationGuard.has_typed_outcome(authority):
                return await accepted(draft, "failed_open", 0, reason)
            return await self._fallback(
                audit=audit,
                run=run,
                authority=authority,
                reason=reason,
                repair_attempts=0,
                telemetry=telemetry,
                status="failed_open" if settings.NARRATION_VALIDATOR_FAIL_OPEN else "safe_fallback",
            )

        if validation_selection is None:
            return await validator_down("validator routing unavailable")

        validator = TurnAuthorityValidator(self._router)
        check = {
            "validator": validator,
            "audit": audit,
            "run": run,
            "selection": validation_selection,
            "authority": authority,
        }
        try:
            beat_failure = self._beat_failure(authority, draft, beat)
            repeats = await self._repeats(campaign_id, draft)
            result = None if beat_failure or repeats else await self._check(
                **check, candidate=draft, attempt_index=0
            )
            if result and result.verdict == "pass":
                return await accepted(draft, "passed", 0)

            record_decision(
                "repair", "requested", {"strategy": "single_model_repair"},
                role=ModelRole.NARRATOR.value,
            )
            repair_messages = [
                *narrator_messages,
                ChatMessage(
                    role="user",
                    content=validator.repair_prompt(authority, draft, result, beat_failure, repeats),
                ),
            ]
            try:
                repaired, repaired_beat, repair_telemetry = await self._narrate(
                    repair_messages,
                    narrator_selection,
                    authority,
                    temperature=settings.NARRATION_REPAIR_TEMPERATURE,
                )
            except LLMProviderError as exc:
                return await self._fallback(
                    audit=audit,
                    run=run,
                    authority=authority,
                    reason=f"repair narrator failed: {type(exc).__name__}: {exc}",
                    repair_attempts=1,
                    telemetry=telemetry,
                )
            telemetry = {**telemetry, "repair_generation": repair_telemetry}
            repaired_failure = self._beat_failure(authority, repaired, repaired_beat)
            repeats = await self._repeats(campaign_id, repaired)
            repaired_result = None if repaired_failure or repeats else await self._check(
                **check, candidate=repaired, attempt_index=1
            )
            if repaired_result and repaired_result.verdict == "pass":
                return await accepted(repaired, "repaired", 1)
            return await self._fallback(
                audit=audit,
                run=run,
                authority=authority,
                reason=(
                    f"beat grant not honored after one repair: {repaired_failure}"
                    if repaired_failure
                    else f"verbatim repeat after one repair: {repeats[0]}" if repeats
                    else repaired_result.summary or "narration still breaks a ban after one repair"
                ),
                repair_attempts=1,
                telemetry=telemetry,
            )
        except NarrationValidationError as exc:
            return await validator_down(f"authority validator failed: {exc}")


__all__ = ["AuthorityNarrationPipeline", "AuthorityNarrationResult"]
