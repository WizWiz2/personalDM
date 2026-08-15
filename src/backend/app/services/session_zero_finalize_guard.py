from __future__ import annotations

_INSTALLED = False


def install() -> None:
    """Make the Session Zero materialization command safe to retry.

    The conversational service historically creates the starting location and hero before it
    delegates to SessionZeroService.complete(). If a client retries `finalize` after a successful
    commit, doing that work again violates the per-campaign entity-name uniqueness constraint.
    Keep the compatibility implementation intact, but short-circuit retries at its command
    boundary and return the already completed playable start.
    """
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    from app.services.session_zero_agent import SessionZeroInterviewService

    original_finalize = SessionZeroInterviewService.finalize

    async def idempotent_finalize(self, campaign_id):
        setup = await self._session_zero.get(campaign_id)
        if setup.status == "completed":
            return await self._session_zero.complete(campaign_id)
        return await original_finalize(self, campaign_id)

    SessionZeroInterviewService.finalize = idempotent_finalize


__all__ = ["install"]
