from __future__ import annotations

from app.db.repositories.fact_memory_repo import FactMemoryRepository
from app.db.repositories.fact_repo import FactRepository
from app.db.repositories.narrative_detail_repo import NarrativeDetailRepository
from app.models.memory_semantics import MemoryClass
from app.models.turn import ChatMessage
from app.services.context_compiler import ContextCompiler, count_tokens
from app.services.scene_bridge_service import SceneBridgeService
from app.services.scene_state_service import SceneStateService

_INSTALLED = False
_ORIGINAL_COMPILE_CONTEXT = ContextCompiler.compile_context
_FACT_MARKER = "\n\n[Campaign Facts & History]\n"


def _remove_legacy_fact_package(content: str) -> str:
    start = content.find(_FACT_MARKER)
    if start < 0:
        return content
    next_package = content.find("\n\n[", start + len(_FACT_MARKER))
    if next_package < 0:
        return content[:start]
    return content[:start] + content[next_package:]


def _fact_line(fact) -> str:
    value = f" {fact.object_value}" if fact.object_value else ""
    return f"- {fact.subject} {fact.predicate}{value}\n"


async def _memory_packages(
    compiler: ContextCompiler,
    campaign_id,
    scene_id,
    state,
    *,
    actor_mode: bool,
    acting_character_id,
    include_facts: bool,
) -> tuple[str, dict]:
    sections: list[str] = []
    manifest = {
        "world_canon_fact_ids": [],
        "entity_state_fact_ids": [],
        "scene_state_fact_ids": [],
        "narrative_detail_ids": [],
    }

    if include_facts:
        facts = await FactRepository(compiler._session).list_active(
            campaign_id,
            visibility="public" if actor_mode else None,
            scene_id=scene_id,
        )
        links = await FactMemoryRepository(compiler._session).get_many(
            [fact.id for fact in facts]
        )
        relevant_entities = set(state.participant_ids) | set(state.object_ids)
        if state.location_id:
            relevant_entities.add(state.location_id)
        if acting_character_id:
            relevant_entities.add(acting_character_id)

        buckets = {
            MemoryClass.WORLD_CANON: [],
            MemoryClass.ENTITY_STATE: [],
            MemoryClass.SCENE_STATE: [],
        }
        for fact in facts:
            linked = links.get(fact.id)
            if linked:
                memory_class, subject_entity_id = linked
            else:
                memory_class = (
                    MemoryClass.SCENE_STATE
                    if fact.scope == "scene"
                    else MemoryClass.WORLD_CANON
                )
                subject_entity_id = None
            if (
                memory_class == MemoryClass.ENTITY_STATE
                and (
                    not subject_entity_id
                    or subject_entity_id not in relevant_entities
                )
            ):
                continue
            if memory_class == MemoryClass.SCENE_STATE and fact.scene_id != scene_id:
                continue
            buckets[memory_class].append(fact)

        for memory_class, heading, key in (
            (MemoryClass.WORLD_CANON, "[World Canon]", "world_canon_fact_ids"),
            (
                MemoryClass.ENTITY_STATE,
                "[Relevant Entity State]",
                "entity_state_fact_ids",
            ),
            (
                MemoryClass.SCENE_STATE,
                "[Current Scene State Memory]",
                "scene_state_fact_ids",
            ),
        ):
            bucket = buckets[memory_class]
            if not bucket:
                continue
            sections.append(
                heading + "\n" + "".join(_fact_line(fact) for fact in bucket)
            )
            manifest[key] = [str(fact.id) for fact in bucket]

    details = await NarrativeDetailRepository(compiler._session).list_recent(
        campaign_id,
        scene_id,
        acting_character_id=acting_character_id if actor_mode else None,
        max_items=6,
    )
    if details:
        sections.append(
            "[Recent Narrative Details — transient, not canon]\n"
            + "".join(f"- {detail.text}\n" for detail in details)
            + "Use these only for local continuity. Do not promote them to facts.\n"
        )
        manifest["narrative_detail_ids"] = [str(detail.id) for detail in details]

    return "\n\n".join(sections), manifest


async def _compile_context_with_scene_state(self, *args, **kwargs):
    messages, metadata = await _ORIGINAL_COMPILE_CONTEXT(self, *args, **kwargs)
    campaign_id = kwargs.get("campaign_id")
    scene_id = kwargs.get("scene_id")
    acting_character_id = kwargs.get("acting_character_id")
    if campaign_id is None and args:
        campaign_id = args[0]
    if acting_character_id is None and len(args) >= 2:
        acting_character_id = args[1]
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
    base_content = _remove_legacy_fact_package(first.content)
    include_facts = "layer_3_facts" in (metadata.get("included_layers") or [])
    memory_text, memory_manifest = await _memory_packages(
        self,
        campaign_id,
        scene_id,
        state,
        actor_mode=acting_character_id is not None,
        acting_character_id=acting_character_id,
        include_facts=include_facts,
    )

    contracts = [SceneStateService.prompt_contract(state)]
    if bridge:
        contracts.append(SceneBridgeService.prompt_contract(bridge))
    additions = [value for value in (memory_text, *contracts) if value]
    enriched_content = base_content
    if additions:
        enriched_content += "\n\n" + "\n\n".join(additions)
    messages = [ChatMessage(role="system", content=enriched_content), *rest]

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
    enriched["memory_manifest"] = memory_manifest
    enriched["included_fact_ids"] = [
        *memory_manifest["world_canon_fact_ids"],
        *memory_manifest["entity_state_fact_ids"],
        *memory_manifest["scene_state_fact_ids"],
    ]
    enriched["included_narrative_detail_ids"] = memory_manifest[
        "narrative_detail_ids"
    ]
    enriched["token_budget_used"] = count_tokens(enriched_content) + sum(
        count_tokens(message.content) for message in rest
    )

    layers = [
        layer
        for layer in list(enriched.get("included_layers") or [])
        if layer != "layer_3_facts"
    ]
    for key, layer in (
        ("world_canon_fact_ids", "layer_3_world_canon"),
        ("entity_state_fact_ids", "layer_3_entity_state"),
        ("scene_state_fact_ids", "layer_3_scene_state"),
        ("narrative_detail_ids", "layer_3_narrative_detail"),
    ):
        if memory_manifest[key] and layer not in layers:
            layers.append(layer)
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
