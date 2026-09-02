from __future__ import annotations

import re
from functools import wraps

from app.services.narration_publication_guard import NarrationPublicationGuard
from app.services.scene_transition_executor import SceneTransitionExecutor
from app.services.turn_authority_service import TurnAuthorityError, TurnAuthorityService
from app.services.turn_planner import TurnPlanningError
from app.services.turn_saga import TurnSaga

_INSTALLED = False


class DeadTurnError(RuntimeError):
    """Raised when the runtime has no concrete typed result it can honestly publish."""


_DEAD_TURN_PATTERN = re.compile(
    r"^(?:пока\s+)?ничего(?:\s+заметно)?\s+не\s+(?:меняется|происходит)[.!?…]*$",
    flags=re.IGNORECASE,
)


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
    if plan.narration_policy.pending_player_choice:
        return False
    return True


def _is_dead_surface(value: object) -> bool:
    clean = " ".join(str(value or "").split()).strip()
    return bool(clean and _DEAD_TURN_PATTERN.fullmatch(clean))


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

    original_apply = SceneTransitionExecutor.apply

    @wraps(original_apply)
    async def strict_transition(self, campaign_id, source_scene_id, trigger_turn_id, plan, *args, **kwargs):
        try:
            return await original_apply(
                self,
                campaign_id,
                source_scene_id,
                trigger_turn_id,
                plan,
                *args,
                **kwargs,
            )
        except ValueError as exc:
            # Interactive transitions used to be converted by TurnSaga into the same empty
            # conservative plan. Translate only that production boundary so the saga's outer
            # compensation/failure path owns the error. Administrative/direct executor callers
            # keep their precise ValueError behavior.
            if trigger_turn_id is None:
                raise
            raise TurnPlanningError(f"Structured scene transition rejected: {exc}") from exc

    SceneTransitionExecutor.apply = strict_transition

    original_build = TurnAuthorityService.build

    @wraps(original_build)
    async def strict_authority(self, *args, **kwargs):
        try:
            return await original_build(self, *args, **kwargs)
        except TurnAuthorityError as exc:
            # TurnSaga names this argument. Other/direct callers may use positional arguments and
            # must keep the original exception semantics instead of being silently reclassified.
            if "acting_character_id" not in kwargs or kwargs["acting_character_id"] is not None:
                raise
            raise TurnPlanningError(f"TurnAuthority rejected the planned turn: {exc}") from exc

    TurnAuthorityService.build = strict_authority

    original_fragment = NarrationPublicationGuard._player_facing_fragment.__func__

    @classmethod
    def strict_fragment(cls, value: object):
        fragment = original_fragment(cls, value)
        if fragment and _is_dead_surface(fragment):
            return None
        return fragment

    NarrationPublicationGuard._player_facing_fragment = strict_fragment

    original_projection = NarrationPublicationGuard._safe_authority_projection.__func__

    @classmethod
    def strict_projection(cls, authority):
        projected = original_projection(cls, authority)
        if _is_dead_surface(projected):
            raise DeadTurnError(
                "TurnAuthority has no player-facing typed outcome; refusing generic no-change fallback"
            )
        return projected

    NarrationPublicationGuard._safe_authority_projection = strict_projection


__all__ = ["DeadTurnError", "_is_dead_surface", "_is_empty_plan", "install"]
