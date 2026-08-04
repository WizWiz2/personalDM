from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.repositories.provider_config_repo import ProviderConfigRepository
from app.db.tables import Campaign, Turn
from app.providers.llm_provider import LLMProvider, LLMProviderError
from app.services.narration_validator import (
    NarrationValidationError,
    NarrationValidator,
)
from app.services.role_model_router import ModelRole, RoleModelRouter


@dataclass(frozen=True)
class NarrationRequestContext:
    campaign_id: UUID
    existing_user_turn_id: UUID | None


class NarrationPipelineProvider:
    """Generate narrator prose, validate it, repair it, then expose accepted text.

    This is an explicit TurnRunner dependency. It never patches LLMProvider or relies
    on import order. The wrapped raw provider remains usable by every other model role.
    """

    STAGES = ("generate_draft", "validate", "repair", "publish_accepted")

    def __init__(
        self,
        session: AsyncSession,
        provider: LLMProvider | None = None,
    ) -> None:
        self._session = session
        self._provider = provider or LLMProvider()
        self._context: NarrationRequestContext | None = None

    @property
    def last_telemetry(self) -> dict | None:
        return self._provider.last_telemetry

    @last_telemetry.setter
    def last_telemetry(self, value: dict | None) -> None:
        self._provider.last_telemetry = value

    @asynccontextmanager
    async def bind(
        self,
        campaign_id: UUID,
        existing_user_turn_id: UUID | None,
    ):
        previous = self._context
        self._context = NarrationRequestContext(
            campaign_id=campaign_id,
            existing_user_turn_id=existing_user_turn_id,
        )
        try:
            yield self
        finally:
            self._context = previous

    async def _collect_raw(
        self,
        messages,
        config,
        api_key,
        **kwargs,
    ) -> str:
        chunks: list[str] = []
        async for token in self._provider.generate_stream(
            messages,
            config,
            api_key,
            **kwargs,
        ):
            chunks.append(token)
        return "".join(chunks)

    async def _current_turn_and_scene(
        self,
        context: NarrationRequestContext,
    ) -> tuple[UUID, UUID | None]:
        trigger_turn_id = context.existing_user_turn_id
        if trigger_turn_id is None:
            row = (
                await self._session.execute(
                    select(Turn.id)
                    .where(
                        Turn.campaign_id == str(context.campaign_id),
                        Turn.role == "user",
                        Turn.status == "active",
                    )
                    .order_by(Turn.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if row is None:
                raise NarrationValidationError("active trigger user turn not found")
            trigger_turn_id = UUID(row)

        campaign = await self._session.get(Campaign, str(context.campaign_id))
        scene_id = (
            UUID(campaign.current_scene_id)
            if campaign and campaign.current_scene_id
            else None
        )
        return trigger_turn_id, scene_id

    @staticmethod
    def _continuation_prefix(messages) -> str:
        if len(messages) < 2:
            return ""
        last = messages[-1]
        previous = messages[-2]
        if (
            getattr(last, "role", None) == "user"
            and "Продолжи ответ ровно с места обрыва"
            in getattr(last, "content", "")
            and getattr(previous, "role", None) == "assistant"
        ):
            return getattr(previous, "content", "")
        return ""

    @staticmethod
    def _attempts(run) -> list[dict]:
        return json.loads(run.attempts_json or "[]")

    @staticmethod
    def _gate_metadata(gate) -> dict:
        return {
            "status": gate.status,
            "validation_run_id": str(gate.validation_run_id),
            "repair_attempts": gate.repair_attempts,
            "violation_count": gate.violation_count,
            "failure_reason": gate.failure_reason,
            "attempts": gate.attempts,
            "buffered_before_delivery": True,
        }

    async def generate_stream(
        self,
        messages,
        config,
        api_key,
        **kwargs,
    ) -> AsyncIterator[str]:
        context = self._context
        if context is None:
            async for token in self._provider.generate_stream(
                messages,
                config,
                api_key,
                **kwargs,
            ):
                yield token
            return

        draft = await self._collect_raw(messages, config, api_key, **kwargs)
        narrator_telemetry = dict(self.last_telemetry or {})
        trigger_turn_id, scene_id = await self._current_turn_and_scene(context)
        router = RoleModelRouter(ProviderConfigRepository(self._session))
        selection = await router.resolve(
            context.campaign_id,
            ModelRole.NARRATION_VALIDATOR,
            config,
        )
        validator = NarrationValidator(self._session, router)
        run = await validator.start_run(
            context.campaign_id,
            trigger_turn_id,
            scene_id,
            draft,
            selection.config.model_name if selection else None,
        )

        if selection is None:
            reason = "narration validator model routing is unavailable"
            gate = await validator.finalize(
                run,
                status=(
                    "failed_open"
                    if settings.NARRATION_VALIDATOR_FAIL_OPEN
                    else "rejected"
                ),
                final_text=draft if settings.NARRATION_VALIDATOR_FAIL_OPEN else None,
                repair_attempts=0,
                failure_reason=reason,
            )
            if not settings.NARRATION_VALIDATOR_FAIL_OPEN:
                raise LLMProviderError(reason)
            self.last_telemetry = {
                **narrator_telemetry,
                "narration_validation": self._gate_metadata(gate),
            }
            yield draft
            return

        candidate = draft
        prefix = self._continuation_prefix(messages)
        repair_attempts = 0
        try:
            while True:
                validation_candidate = f"{prefix}{candidate}" if prefix else candidate
                result = await validator.validate(
                    selection,
                    messages,
                    validation_candidate,
                )
                await validator.record_attempt(
                    run,
                    attempt_index=len(self._attempts(run)),
                    candidate_text=validation_candidate,
                    result=result,
                    telemetry=validator.telemetry,
                )
                if result.verdict == "pass":
                    status = "repaired" if repair_attempts else "passed"
                    gate = await validator.finalize(
                        run,
                        status=status,
                        final_text=validation_candidate,
                        repair_attempts=repair_attempts,
                    )
                    self.last_telemetry = {
                        **dict(self.last_telemetry or narrator_telemetry),
                        "narration_validation": self._gate_metadata(gate),
                    }
                    # BaseTurnRunner merges a continuation with its prefix itself.
                    yield candidate
                    return

                if prefix:
                    gate = await validator.finalize(
                        run,
                        status="rejected",
                        final_text=None,
                        repair_attempts=repair_attempts,
                        failure_reason=(
                            "A truncated full response violated narration constraints; "
                            "restart generation instead of repairing only its suffix."
                        ),
                    )
                    self.last_telemetry = {
                        **narrator_telemetry,
                        "narration_validation": self._gate_metadata(gate),
                    }
                    raise LLMProviderError(
                        gate.failure_reason or "narration rejected"
                    )

                if repair_attempts >= settings.NARRATION_REPAIR_ATTEMPTS:
                    gate = await validator.finalize(
                        run,
                        status="rejected",
                        final_text=None,
                        repair_attempts=repair_attempts,
                        failure_reason=result.summary or "narration remained invalid",
                    )
                    self.last_telemetry = {
                        **narrator_telemetry,
                        "narration_validation": self._gate_metadata(gate),
                    }
                    raise LLMProviderError(
                        "Narration rejected after repair: "
                        f"{gate.failure_reason or 'continuity violation'}"
                    )

                candidate = await self._collect_raw(
                    validator.repair_messages(messages, candidate, result),
                    config,
                    api_key,
                    temperature=settings.NARRATION_REPAIR_TEMPERATURE,
                )
                repair_attempts += 1
                run.repair_attempts = repair_attempts
                await self._session.flush()
        except NarrationValidationError as exc:
            status = (
                "failed_open"
                if settings.NARRATION_VALIDATOR_FAIL_OPEN
                else "rejected"
            )
            gate = await validator.finalize(
                run,
                status=status,
                final_text=draft if settings.NARRATION_VALIDATOR_FAIL_OPEN else None,
                repair_attempts=repair_attempts,
                failure_reason=str(exc)[:2000],
            )
            self.last_telemetry = {
                **narrator_telemetry,
                "narration_validation": self._gate_metadata(gate),
            }
            if not settings.NARRATION_VALIDATOR_FAIL_OPEN:
                raise LLMProviderError(f"Narration validator failed: {exc}") from exc
            yield draft
