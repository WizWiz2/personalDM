from __future__ import annotations

from app.services.role_model_router import ModelRole
from app.services.session_zero_agent import SessionZeroInterviewIncompleteError

_INSTALLED = False

_PLACEHOLDER_LOCATIONS = frozenset(
    {
        "стартовая локация",
        "начальная локация",
        "место старта",
        "неизвестная локация",
        "неуточненная локация",
        "starting location",
        "start location",
        "unknown location",
    }
)


def _key(value: object) -> str:
    return " ".join(str(value or "").casefold().replace("ё", "е").split())


def is_placeholder_location(value: object) -> bool:
    return _key(value) in _PLACEHOLDER_LOCATIONS


def _latest_user_message(state) -> str:
    for item in reversed(state.messages):
        if item.get("role") == "user" and item.get("content"):
            return str(item["content"])
    return ""


def install() -> None:
    """Fail closed before Session Zero can materialize a synthetic location identity.

    The autonomous-start fallback historically filled a missing location with the literal
    ``Стартовая локация``. Because finalize() turns that field into a durable Location entity, a
    transport/model failure could permanently canonize a UI placeholder. Give the structured start
    materializer one more chance, but never persist the placeholder itself.
    """
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    from app.services.session_zero_agent import SessionZeroInterviewService as BaseInterviewService

    original_finalize = BaseInterviewService.finalize

    async def guarded_finalize(self, campaign_id):
        state = await self.get_state(campaign_id)
        location_name = state.draft.world.starting_location_name
        if not is_placeholder_location(location_name):
            return await original_finalize(self, campaign_id)

        repaired = state.draft.model_copy(deep=True)
        repaired.world.starting_location_name = None
        selection = await self._router.resolve(campaign_id, ModelRole.SESSION_ZERO)
        materialize_start = getattr(self, "_materialize_start", None)
        if selection is not None and callable(materialize_start):
            repaired = await materialize_start(
                selection,
                state,
                repaired,
                _latest_user_message(state),
            )

        if is_placeholder_location(repaired.world.starting_location_name):
            repaired.world.starting_location_name = None
        if not str(repaired.world.starting_location_name or "").strip():
            state.draft = repaired
            save_state = getattr(self, "_save_state", None)
            if callable(save_state):
                await save_state(campaign_id, state, commit=True)
            raise SessionZeroInterviewIncompleteError(["world.starting_location_name"])

        state.draft = repaired
        save_state = getattr(self, "_save_state", None)
        if callable(save_state):
            await save_state(campaign_id, state, commit=True)
        return await original_finalize(self, campaign_id)

    BaseInterviewService.finalize = guarded_finalize


__all__ = ["install", "is_placeholder_location"]
