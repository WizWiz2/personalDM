from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from pydantic import Field, create_model
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.repositories.provider_config_repo import ProviderConfigRepository
from app.db.tables import Belief, Entity
from app.models.narration_validation import (
    GrantedBeat,
    GrantedNarration,
    NarrationValidationResult,
    RouteDisclosure,
)
from app.models.turn import ChatMessage
from app.models.turn_authority import TurnAuthority
from app.providers.llm_provider import (
    LLMProvider,
    LLMProviderError,
    LLMProviderTruncatedError,
)
from app.services.actor_turn_authority_guard import _word_key, segment_actor_response
from app.services.llm_usage_tracker import record_decision, record_provider_telemetry
from app.services.narration_meta_guard import leaked_meta_lines
from app.services.narration_publication_guard import (
    NarrationPublicationError,
    NarrationPublicationGuard,
)
from app.services.narration_validator import NarrationValidationError, NarrationValidator
from app.services.role_model_router import ModelRole, RoleModelRouter, RoleModelSelection
from app.services.script_consistency import consistent_script, foreign_script_intrusions
from app.services.turn_authority_validator import TurnAuthorityValidator


class NarrationRefused(RuntimeError):
    """Honest failure: nothing is published and the saga rolls the turn back."""


NARROWED_CONTRACT = (
    "[NARROWED CONTRACT]\nThe previous narration was rejected: {reason}\n"
    "Narrate ONLY the confirmed outcomes below, in your own voice and style as this master, "
    "second person, in Russian. Add no new people, objects, places, facts or player actions.\n"
    "CONFIRMED OUTCOMES:\n{contract}"
)


@dataclass(frozen=True)
class AuthorityNarrationResult:
    text: str
    telemetry: dict
    validation_run_id: UUID | None = None
    validation_status: str = "not_invoked"
    beat: GrantedBeat | None = None


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
        from app.services.published_routes import route_catalog

        catalog = await route_catalog(self._session, authority.campaign_id)
        destinations = tuple(f"{kind}:{place['id']}" for place in catalog for kind in ("known", "new_in"))
        route = create_model("RegisteredRouteDisclosure", __base__=RouteDisclosure,
                             destination=(Literal[destinations] if destinations else str, ...))
        owner = create_model("GrantedBeat", __base__=GrantedBeat,
                             cast_id=(Literal[str(authority.beat_owner_id)], ...),
                             route_disclosures=(list[route], Field(default_factory=list, max_length=2)))
        wire = create_model("GrantedNarration", __base__=GrantedNarration, beat=(owner, ...))
        if catalog:
            import json

            messages = [*messages, ChatMessage(role="system", content=(
                "[ROUTE DISCLOSURES]\nRegistered places: " + json.dumps(catalog, ensure_ascii=False)
                + "\nWhen the beat owner gives definite directions from the CURRENT scene place to "
                  "a public destination, include route_disclosures in the beat. Select known:<ID> "
                  "for an existing destination; new_in:<parent ID> for a genuinely new place inside "
                  "a registered area, with its nominative name and physical profile. Reuse names/aliases. "
                  "evidence must be an exact fragment of beat.evidence containing those directions. "
                  "Include the full directions in beat.evidence. Ordinary directions are bidirectional=true. "
                  "Only an explicit physical obstacle to returning permits bidirectional=false; "
                  "one_way_evidence must quote that obstacle from the directions, otherwise null. "
                  "Use [] for rumours, lies, speculation, mere mentions, inaccessible destinations, "
                  "or routes starting elsewhere. This records knowledge, never movement or permission "
                  "to open a blocked passage."
            ))]
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
        require_outcomes: bool = False,
    ) -> NarrationValidationResult:
        result = await validator.validate(selection, authority, candidate,
            **({"require_outcomes": True} if require_outcomes else {}))
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
        validation_result: NarrationValidationResult | None = None,
        reason: str | None = None,
        beat: GrantedBeat | None = None,
    ) -> AuthorityNarrationResult:
        if leaked_meta_lines(text):
            raise NarrationValidationError("Narrator output contains internal model notes")
        text = consistent_script(text, authority.present_character_names)
        text = NarrationPublicationGuard.include_critical_world_outcome(authority, text)
        gate = await audit.finalize(
            run,
            status=status,
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
            beat=beat,
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
            raise NarrationRefused(f"narrator failed: {type(exc).__name__}: {exc}") from exc
        validation_selection = await self._router.resolve(
            campaign_id,
            ModelRole.NARRATION_VALIDATOR,
            narrator_selection.config,
        )
        validator_model = validation_selection.config.model_name if validation_selection else None
        run = await audit.start_run(campaign_id, trigger_turn_id, scene_id, draft, validator_model)

        async def accepted(text: str, status: str, attempts: int, reason: str | None = None,
                           beat=None, validation=None, narrowed: bool = False):
            _, publication = NarrationPublicationGuard.publish(authority, text, validation)
            if publication["mode"] != "validated_candidate":
                reason_now = "item state not certified by the typed check"
                if narrowed:
                    raise NarrationRefused(reason_now)
                # An uncertified item claim: the typed outcome becomes the next contract.
                return await narrow(reason_now, attempts)
            record_decision("publish", "narrowed_candidate" if narrowed else "validated_candidate",
                            {"status": status})
            return await self._finish(
                audit=audit,
                run=run,
                authority=authority,
                text=text.strip(),
                status=status,
                repair_attempts=attempts,
                telemetry=telemetry,
                publication={"mode": "narrowed_candidate" if narrowed else "validated_candidate",
                             "validated_surface": True,
                             "outcomes_verified": bool(validation and validation._outcomes_verified)},
                validation_result=validation,
                reason=reason,
                beat=beat,
            )

        async def narrow(reason: str, attempts: int):
            """The typed outcome is a contract for one more narrator pass, never a publication."""
            record_decision("repair", "narrowed_contract", {"strategy": "narrowed_contract", "reason": reason[:500]},
                            role=ModelRole.NARRATOR.value)
            try:
                contract = NarrationPublicationGuard.render_contract(authority)
            except NarrationPublicationError as exc:
                raise NarrationRefused(f"{reason}; {exc}") from exc
            try:
                text, _, _ = await self._narrate(
                    [*narrator_messages, ChatMessage(role="user", content=NARROWED_CONTRACT.format(
                        reason=reason[:600], contract=contract))],
                    narrator_selection, authority, temperature=settings.NARRATION_REPAIR_TEMPERATURE,
                )
            except LLMProviderError as exc:
                raise NarrationRefused(f"{reason}; narrowed narrator failed: {exc}") from exc
            if not (foreign_script_intrusions(text) or leaked_meta_lines(text)
                    or await self._repeats(campaign_id, text)):
                result = await self._check(**check, candidate=text, attempt_index=attempts + 1,
                                           require_outcomes=True)
                if result.verdict == "pass":
                    return await accepted(text, "repaired", attempts + 1, reason=reason,
                                          validation=result, narrowed=True)
            raise NarrationRefused(f"{reason}; narrowed retry did not pass")

        if validation_selection is None:
            raise NarrationRefused("validator routing unavailable")

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
            # Foreign-script garbage words are decoder noise, not prose: like a verbatim
            # repeat they disqualify the candidate from the surface, and the repair call
            # is told exactly which words to replace (playtest 2026-10-12: CJK tokens
            # reached published text because consistent_script only fixes mixed words).
            intrusions = [*foreign_script_intrusions(draft), *leaked_meta_lines(draft)]
            # A missed beat grant is an unhonored initiative decoration, not a forbidden
            # fact. When only the beat failed, the four-ban validator still guards the
            # authority surface: validated prose must reach the player instead of the
            # deterministic projection (playtest 2026-10-12: most safe_fallbacks were
            # otherwise-clean candidates lost to a grammatical-subject miss).
            if repeats or intrusions:
                result = None
            else:
                result = await self._check(**check, candidate=draft, attempt_index=0)
                if result.verdict == "pass":
                    if beat_failure:
                        return await accepted(
                            draft, "passed", 0, reason=f"beat_unhonored: {beat_failure}", validation=result,
                        )
                    return await accepted(draft, "passed", 0, beat=beat, validation=result)

            record_decision(
                "repair", "requested", {"strategy": "single_model_repair"},
                role=ModelRole.NARRATOR.value,
            )
            repair_messages = [
                *narrator_messages,
                ChatMessage(
                    role="user",
                    content=validator.repair_prompt(
                        authority, draft, result, beat_failure, repeats,
                        script_intrusions=intrusions,
                    ),
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
                return await narrow(f"repair narrator failed: {type(exc).__name__}: {exc}", 1)
            telemetry = {**telemetry, "repair_generation": repair_telemetry}
            repaired_failure = self._beat_failure(authority, repaired, repaired_beat)
            repeats = await self._repeats(campaign_id, repaired)
            repaired_intrusions = [
                *foreign_script_intrusions(repaired), *leaked_meta_lines(repaired)
            ]
            if repeats or repaired_intrusions:
                repaired_result = None
            else:
                repaired_result = await self._check(
                    **check, candidate=repaired, attempt_index=1, require_outcomes=True,
                )
                if repaired_result.verdict == "pass":
                    if repaired_failure:
                        return await accepted(
                            repaired, "repaired", 1,
                            reason=f"beat_unhonored: {repaired_failure}",
                            validation=repaired_result,
                        )
                    return await accepted(repaired, "repaired", 1, beat=repaired_beat, validation=repaired_result)
            return await narrow(
                (
                    # The four-ban validator is authoritative: a failed NPC beat
                    # must not mask a real canon/player-agency violation in the
                    # published audit reason.
                    (repaired_result.summary or "narration still breaks a ban after one repair")
                    if repaired_result is not None and repaired_result.verdict != "pass"
                    else f"verbatim repeat after one repair: {repeats[0]}"
                    if repeats
                    else "foreign-script intrusions after one repair: " + ", ".join(repaired_intrusions)
                    if repaired_intrusions
                    else f"beat grant not honored after one repair: {repaired_failure}"
                    if repaired_failure
                    else "narration still breaks a ban after one repair"
                ),
                1,
            )
        except NarrationValidationError as exc:
            # Validator timeout/failure is fail-closed: an unchecked draft is never published.
            raise NarrationRefused(f"authority validator failed: {exc}") from exc


__all__ = ["AuthorityNarrationPipeline", "AuthorityNarrationResult"]
