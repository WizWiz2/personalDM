from __future__ import annotations

from app.models.turn import ChatMessage
from app.services.context_compiler import ContextCompiler
from app.services.scene_bridge_service import SceneBridgeService
from app.services.scene_state_service import SceneStateService

_INSTALLED = False
_ORIGINAL_COMPILE_CONTEXT = ContextCompiler.compile_context


async def _compile_context_with_scene_state(self, *args, **kwargs):
    messages, metadata = await _ORIGINAL_COMPILE_CONTEXT(self, *args, **kwargs)
    campaign_id = kwargs.get("campaign_id")
    scene_id = kwargs.get("scene_id")
    if campaign_id is None and args:
        campaign_id = args[0]
    if scene_id is None and len(args) >= 3:
        scene_id = args[2]
    if not campaign_id or not scene_id or not messages:
        return messages, metadata

    try:
        state = await SceneStateService(self._session).get(campaign_id, scene_id)
    except ValueError:
        return messages, metadata

    bridge = await SceneBridgeService(self._session).get_for_target_scene(
        campaign_id,
        scene_id,
    )
    first, *rest = messages
    contracts = [SceneStateService.prompt_contract(state)]
    if bridge:
        contracts.append(SceneBridgeService.prompt_contract(bridge))
    messages = [
        ChatMessage(
            role="system",
            content=f"{first.content}\n\n" + "\n".join(contracts),
        ),
        *rest,
    ]
    enriched = dict(metadata)
    enriched["scene_state"] = {
        "scene_id": str(state.scene_id),
        "location_id": str(state.location_id) if state.location_id else None,
        "location_path": state.location_path,
        "world_time_label": state.world_time_label,
        "world_time_order": state.world_time_order,
        "participant_ids": [str(value) for value in state.participant_ids],
        "object_ids": [str(value) for value in state.object_ids],
        "available_exit_ids": [str(item.id) for item in state.available_exits],
        "available_destination_ids": [
            str(item.to_location_id) for item in state.available_exits
        ],
        "invariant_errors": state.invariant_errors,
    }
    if bridge:
        enriched["scene_bridge"] = bridge.model_dump(mode="json")
    layers = list(enriched.get("included_layers") or [])
    if "layer_1_authoritative_scene_state" not in layers:
        layers.append("layer_1_authoritative_scene_state")
    if bridge and "layer_1_scene_bridge" not in layers:
        layers.append("layer_1_scene_bridge")
    enriched["included_layers"] = layers
    return messages, enriched


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    ContextCompiler.compile_context = _compile_context_with_scene_state
    _INSTALLED = True
