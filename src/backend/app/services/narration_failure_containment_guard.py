from __future__ import annotations

from uuid import UUID

from sqlalchemy import select

from app.db.narration_validation_table import NarrationValidationRun
from app.providers.llm_provider import LLMProviderError
from app.services.authority_narration_pipeline import (
    AuthorityNarrationPipeline,
    AuthorityNarrationResult,
)
from app.services.narration_publication_guard import NarrationPublicationGuard
from app.services.narration_validator import NarrationValidationError

_INSTALLED = False


async def _latest_validating_run(
    pipeline: AuthorityNarrationPipeline,
    trigger_turn_id: UUID,
) -> NarrationValidationRun | None:
    result = await pipeline._session.execute(
        select(NarrationValidationRun)
        .where(
            NarrationValidationRun.trigger_turn_id == str(trigger_turn_id),
            NarrationValidationRun.status == "validating",
        )
        .order_by(NarrationValidationRun.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def recover_presentation_failure(
    pipeline: AuthorityNarrationPipeline,
    *,
    trigger_turn_id: UUID,
    authority,
    error: Exception,
) -> AuthorityNarrationResult:
    """Render committed authority after an LLM-only presentation failure.

    Planner, transition execution, TurnAuthority and outcome materialization have already established
    the game result before narration starts. A narrator/provider/validator transport failure is
    therefore not allowed to compensate that structured result or leak its exception text to the
    player. Database/state failures are intentionally outside this boundary and still abort normally.
    """
    published, publication = NarrationPublicationGuard.publish(authority, "", None)
    reason = f"{type(error).__name__}: {error}"[:2000]

    validation_run_id: UUID | None = None
    run = await _latest_validating_run(pipeline, trigger_turn_id)
    if run is not None:
        run.status = "repaired"
        run.final_text = published
        run.repair_attempts = max(1, int(run.repair_attempts or 0))
        run.failure_reason = reason
        await pipeline._session.commit()
        validation_run_id = UUID(run.id)

    provider_telemetry = dict(pipeline.last_telemetry or {})
    return AuthorityNarrationResult(
        text=published,
        telemetry={
            **provider_telemetry,
            "narration_degraded": True,
            "structured_outcome_preserved": True,
            "narration_validation": {
                "status": "presentation_fallback",
                "validation_run_id": (
                    str(validation_run_id) if validation_run_id is not None else None
                ),
                "authority_version": authority.version,
                "publication_guard": publication,
                "presentation_failure_recovered": True,
                "error_type": type(error).__name__,
                "reason": reason,
            },
        },
        validation_run_id=validation_run_id,
        validation_status="safe_fallback",
    )


def install() -> None:
    """Contain narrator/control-model presentation failures behind TurnAuthority."""
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    original_generate = AuthorityNarrationPipeline.generate

    async def contained_generate(self, *args, **kwargs):
        try:
            return await original_generate(self, *args, **kwargs)
        except (LLMProviderError, NarrationValidationError) as exc:
            authority = kwargs.get("authority")
            trigger_turn_id = kwargs.get("trigger_turn_id")
            if authority is None or trigger_turn_id is None:
                raise
            return await recover_presentation_failure(
                self,
                trigger_turn_id=trigger_turn_id,
                authority=authority,
                error=exc,
            )

    AuthorityNarrationPipeline.generate = contained_generate


__all__ = ["install", "recover_presentation_failure"]
