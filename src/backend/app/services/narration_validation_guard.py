from __future__ import annotations

from collections.abc import AsyncIterator
from contextvars import ContextVar
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select

from app.config import settings
from app.db.repositories.provider_config_repo import ProviderConfigRepository
from app.db.tables import Campaign, Turn
from app.providers.llm_provider import LLMProvider, LLMProviderError
from app.services.narration_validator import (
    NarrationValidationError,
    NarrationValidator,
)
from app.services.role_model_router import ModelRole, RoleModelRouter
from app.services.turn_runner import TurnRunner


@dataclass(frozen=True)
class _ValidationContext:
    runner: TurnRunner
    campaign_id: UUID
    existing_user_turn_id: UUID | None


_CONTEXT: ContextVar[_ValidationContext | None] = ContextVar(
    "narration_validation_context",
    default=None,
)
_IN_REPAIR: ContextVar[bool] = ContextVar(
    "narration_validation_repair",
    default=False,
)
_INSTALLED = False
_ORIGINAL_RUN_TURN_STREAM = TurnRunner.run_turn_stream
_ORIGINAL_GENERATE_STREAM = LLMProvider.generate_stream


async def _run_turn_stream_with_validation(
    self: TurnRunner,
    campaign_id: UUID,
    turn_create,
    existing_user_turn_id: UUID | None = None,
):
    token = _CONTEXT.set(
        _ValidationContext(
            runner=self,
            campaign_id=campaign_id,
            existing_user_turn_id=existing_user_turn_id,
        )
    )
    try:
        async for item in _ORIGINAL_RUN_TURN_STREAM(
            self,
            campaign_id,
            turn_create,
            existing_user_turn_id,
        ):
            yield item
    finally:
        _CONTEXT.reset(token)


async def _collect_original(
    provider: LLMProvider,
    messages,
    config,
    api_key,
    **kwargs,
) -> str:
    chunks: list[str] = []
    async for token in _ORIGINAL_GENERATE_STREAM(
        provider,
        messages,
        config,
        api_key,
        **kwargs,
    ):
        chunks.append(token)
    return "".join(chunks)


async def _current_turn_and_scene(context: _ValidationContext) -> tuple[UUID, UUID | None]:
    session = context.runner._session
    trigger_turn_id = context.existing_user_turn_id
    if trigger_turn_id is None:
        row = (
            await session.execute(
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

    campaign = await session.get(Campaign, str(context.campaign_id))
    scene_id = (
        UUID(campaign.current_scene_id)
        if campaign and campaign.current_scene_id
        else None
    )
    return trigger_turn_id, scene_id


def _continuation_prefix(messages) -> str:
    if len(messages) < 2:
        return ""
    last = messages[-1]
    previous = messages[-2]
    if (
        getattr(last, "role", None) == "user"
        and "Продолжи ответ ровно с места обрыва" in getattr(last, "content", "")
        and getattr(previous, "role", None) == "assistant"
    ):
        return getattr(previous, "content", "")
    return ""


async def _generate_stream_validated(
    self: LLMProvider,
    messages,
    config,
    api_key,
    **kwargs,
) -> AsyncIterator[str]:
    context = _CONTEXT.get()
    if context is None or _IN_REPAIR.get():
        async for token in _ORIGINAL_GENERATE_STREAM(
            self,
            messages,
            config,
            api_key,
            **kwargs,
        ):
            yield token
        return

    draft = await _collect_original(self, messages, config, api_key, **kwargs)
    narrator_telemetry = dict(self.last_telemetry or {})
    trigger_turn_id, scene_id = await _current_turn_and_scene(context)
    router = RoleModelRouter(ProviderConfigRepository(context.runner._session))
    selection = await router.resolve(
        context.campaign_id,
        ModelRole.NARRATION_VALIDATOR,
        config,
    )
    validator = NarrationValidator(context.runner._session, router)
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
            status="failed_open" if settings.NARRATION_VALIDATOR_FAIL_OPEN else "rejected",
            final_text=draft if settings.NARRATION_VALIDATOR_FAIL_OPEN else None,
            repair_attempts=0,
            failure_reason=reason,
        )
        if not settings.NARRATION_VALIDATOR_FAIL_OPEN:
            raise LLMProviderError(reason)
        self.last_telemetry = {
            **narrator_telemetry,
            "narration_validation": _gate_metadata(gate),
        }
        yield draft
        return

    candidate = draft
    prefix = _continuation_prefix(messages)
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
                attempt_index=len(_attempts(run)),
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
                    "narration_validation": _gate_metadata(gate),
                }
                # A continuation is merged by TurnRunner, so only yield its accepted suffix.
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
                    "narration_validation": _gate_metadata(gate),
                }
                raise LLMProviderError(gate.failure_reason or "narration rejected")

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
                    "narration_validation": _gate_metadata(gate),
                }
                raise LLMProviderError(
                    "Narration rejected after repair: "
                    f"{gate.failure_reason or 'continuity violation'}"
                )

            repair_token = _IN_REPAIR.set(True)
            try:
                candidate = await _collect_original(
                    self,
                    validator.repair_messages(messages, candidate, result),
                    config,
                    api_key,
                    temperature=settings.NARRATION_REPAIR_TEMPERATURE,
                )
            finally:
                _IN_REPAIR.reset(repair_token)
            repair_attempts += 1
            run.repair_attempts = repair_attempts
            await context.runner._session.flush()
    except NarrationValidationError as exc:
        status = "failed_open" if settings.NARRATION_VALIDATOR_FAIL_OPEN else "rejected"
        gate = await validator.finalize(
            run,
            status=status,
            final_text=draft if settings.NARRATION_VALIDATOR_FAIL_OPEN else None,
            repair_attempts=repair_attempts,
            failure_reason=str(exc)[:2000],
        )
        self.last_telemetry = {
            **narrator_telemetry,
            "narration_validation": _gate_metadata(gate),
        }
        if not settings.NARRATION_VALIDATOR_FAIL_OPEN:
            raise LLMProviderError(f"Narration validator failed: {exc}") from exc
        yield draft


def _attempts(run) -> list[dict]:
    import json

    return json.loads(run.attempts_json or "[]")


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


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    TurnRunner.run_turn_stream = _run_turn_stream_with_validation
    LLMProvider.generate_stream = _generate_stream_validated
    _INSTALLED = True
