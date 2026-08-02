from __future__ import annotations

from app.services.memory_operations import MemoryOperationsService
from app.services.thesis_curator import ThesisCurator

_INSTALLED = False
_ORIGINAL_RECONCILE = ThesisCurator.reconcile
_ORIGINAL_CLOSE_SCENE = ThesisCurator.close_scene


def _supports_lifecycle(session) -> bool:
    return callable(getattr(session, "execute", None)) and callable(
        getattr(session, "flush", None)
    )


async def _reconcile_with_lifecycle(self, scene_id, source_turn_id, desired):
    result = await _ORIGINAL_RECONCILE(self, scene_id, source_turn_id, desired)
    if _supports_lifecycle(self._session):
        await MemoryOperationsService(self._session).record_reconcile(
            scene_id,
            source_turn_id,
            desired,
        )
    return result


async def _close_scene_with_lifecycle(self, scene_id):
    result = await _ORIGINAL_CLOSE_SCENE(self, scene_id)
    if _supports_lifecycle(self._session):
        await MemoryOperationsService(self._session).record_closed_scene(scene_id)
    return result


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    ThesisCurator.reconcile = _reconcile_with_lifecycle
    ThesisCurator.close_scene = _close_scene_with_lifecycle
    _INSTALLED = True
