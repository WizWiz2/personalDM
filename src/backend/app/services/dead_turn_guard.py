from __future__ import annotations

from functools import wraps

from app.services.narration_publication_guard import NarrationPublicationGuard
from app.services.turn_authority_service import TurnAuthorityService
from app.services.turn_planner import TurnPlanningError
from app.services.turn_saga import TurnSaga

_INSTALLED = False


def _is_empty_plan(plan) -> bool:
    """Only a missing plan object is empty.

    An absent reply, gesture, refusal, step, or consequence is not a refusal.
    Planner crashes are metadata status != completed, handled by the caller.
    """
    return plan is None


def _is_dead_surface(value: object) -> bool:
    clean = " ".join(str(value or "").split()).strip()
    return bool(clean and NarrationPublicationGuard.DEAD_TURN_PATTERN.fullmatch(clean))


def _empty_plan_diagnostic(plan) -> str:
    return (
        f"player_intent={str(getattr(plan, 'player_intent', '') or '')[:240]!r}; "
        f"resolution={getattr(plan, 'resolution', None)!r}; "
        f"action_steps={len(getattr(getattr(plan, 'action_sequence', None), 'steps', ()) or ())}; "
        f"transition_required={bool(getattr(getattr(plan, 'scene_transition', None), 'required', False))}; "
        f"observable_consequences={len(getattr(plan, 'observable_consequences', ()) or ())}; "
        f"character_beats={len(getattr(plan, 'character_beats', ()) or ())}; "
        f"addressed_response_requested={bool(getattr(plan, 'addressed_response_requested', False))}; "
        f"pending_player_choice={getattr(getattr(plan, 'narration_policy', None), 'pending_player_choice', None)!r}"
    )


def install() -> None:
    """Turn control failures must fail/retry, never masquerade as uneventful fiction."""
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    original_plan = TurnSaga._plan

    @wraps(original_plan)
    async def strict_plan(self, *args, **kwargs):
        plan, metadata = await original_plan(self, *args, **kwargs)
        if metadata.get("status") != "completed":
            reason = metadata.get("reason") or metadata.get("status") or "planner unavailable"
            error = metadata.get("error")
            detail = f": {error}" if error else ""
            raise TurnPlanningError(f"Planner did not produce an authoritative turn ({reason}){detail}")
        if _is_empty_plan(plan):
            raise TurnPlanningError(
                "Planner returned no plan object; refusing to publish fiction; "
                + _empty_plan_diagnostic(plan)
            )
        return plan, metadata

    TurnSaga._plan = strict_plan

    original_build = TurnAuthorityService.build

    @wraps(original_build)
    async def strict_authority(self, *args, **kwargs):
        # A completed plan with no typed mark is not a defect. Actor-scoped turns use plan=None
        # and stay outside this rule. Only a missing plan object is empty; a planner crash is
        # metadata status != completed, raised in strict_plan, and must not be published.
        plan = kwargs.get("plan")
        acting_character_id = kwargs.get("acting_character_id")
        if acting_character_id is None and plan is not None and _is_empty_plan(plan):
            raise TurnPlanningError(
                "Typed plan reached authority without a plan object; "
                + _empty_plan_diagnostic(plan)
            )
        return await original_build(self, *args, **kwargs)

    TurnAuthorityService.build = strict_authority


__all__ = ["_empty_plan_diagnostic", "_is_dead_surface", "_is_empty_plan", "install"]
