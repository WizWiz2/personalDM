from __future__ import annotations

from functools import wraps

from app.services.narration_publication_guard import NarrationPublicationGuard
from app.services.turn_authority_service import TurnAuthorityService
from app.services.turn_planner import TurnPlanningError
from app.services.turn_saga import TurnSaga

_INSTALLED = False


def _is_empty_plan(plan) -> bool:
    """Machine-provable absence of a current result; no semantic guessing lives here."""
    if plan is None:
        return True
    if plan.observable_consequences:
        return False
    if plan.action_sequence.steps:
        return False
    if plan.scene_transition.required:
        return False
    if getattr(plan, "addressed_response_requested", False):
        return False
    if plan.character_beats:
        return False
    return not plan.narration_policy.pending_player_choice


def _is_dead_surface(value: object) -> bool:
    clean = " ".join(str(value or "").split()).strip()
    return bool(clean and NarrationPublicationGuard.DEAD_TURN_PATTERN.fullmatch(clean))


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
                "Planner produced no concrete current-world result; refusing an empty narrative turn"
            )
        return plan, metadata

    TurnSaga._plan = strict_plan

    original_build = TurnAuthorityService.build

    @wraps(original_build)
    async def strict_authority(self, *args, **kwargs):
        # TurnSaga's historical transition/authority recovery path replaces a rejected plan with
        # CoordinatedTurnPlan.conservative_fallback() and then calls build() again. Reject that
        # second empty plan before it can become PREPARED world state. Actor-scoped turns legitimately
        # use plan=None and are deliberately outside this rule.
        plan = kwargs.get("plan")
        acting_character_id = kwargs.get("acting_character_id")
        if acting_character_id is None and plan is not None and _is_empty_plan(plan):
            raise TurnPlanningError(
                "Control-plane recovery produced no concrete typed outcome; retry the turn instead"
            )
        return await original_build(self, *args, **kwargs)

    TurnAuthorityService.build = strict_authority


__all__ = ["_is_dead_surface", "_is_empty_plan", "install"]
