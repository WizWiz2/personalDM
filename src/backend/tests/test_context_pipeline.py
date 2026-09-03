from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.turn import ChatMessage
from app.services.context_pipeline import (
    CompiledContext,
    ContextPipeline,
    ContextRequest,
    SceneStateContextProvider,
)


class RecordingProvider:
    def __init__(self, name: str):
        self.name = name

    async def enrich(
        self,
        request: ContextRequest,
        context: CompiledContext,
    ) -> CompiledContext:
        metadata = dict(context.metadata)
        order = list(metadata.get("recorded_order") or [])
        order.append(self.name)
        metadata["recorded_order"] = order
        first, *rest = context.messages
        return CompiledContext(
            messages=[
                ChatMessage(
                    role=first.role,
                    content=f"{first.content}|{self.name}",
                ),
                *rest,
            ],
            metadata=metadata,
        )


@pytest.mark.asyncio
async def test_context_pipeline_applies_providers_in_declared_order() -> None:
    pipeline = ContextPipeline(
        [RecordingProvider("scene"), RecordingProvider("texture")]
    )
    original_messages = [ChatMessage(role="system", content="base")]
    original_metadata = {"included_layers": ["layer_0_system"]}

    messages, metadata = await pipeline.enrich(
        ContextRequest(campaign_id=uuid4()),
        original_messages,
        original_metadata,
    )

    assert messages[0].content == "base|scene|texture"
    assert metadata["recorded_order"] == ["scene", "texture"]
    assert metadata["context_pipeline"] == ["scene", "texture"]
    assert original_messages[0].content == "base"
    assert original_metadata == {"included_layers": ["layer_0_system"]}


class _FakeRows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _ActionReferenceSession:
    def __init__(self, player_id, item_id):
        self.player_id = player_id
        self.item_id = item_id

    async def get(self, _model, _key):
        return SimpleNamespace(player_character_id=str(self.player_id))

    async def execute(self, _query):
        return _FakeRows([(str(self.item_id), "Латунный ключ")])


@pytest.mark.asyncio
async def test_scene_context_exposes_exact_ids_for_single_inventory_mutation() -> None:
    campaign_id = uuid4()
    player_id = uuid4()
    item_id = uuid4()
    npc_id = uuid4()
    provider = SceneStateContextProvider(
        _ActionReferenceSession(player_id, item_id)  # type: ignore[arg-type]
    )
    state = SimpleNamespace(
        participant_ids=[npc_id],
        participant_names=["Мартин Вэнс"],
    )

    contract, owned_ids = await provider._action_reference_contract(
        ContextRequest(campaign_id=campaign_id),
        state,
    )

    assert f"Латунный ключ [id={item_id}]" in contract
    assert f"Мартин Вэнс [id={npc_id}]" in contract
    assert "even when it is the only committed world action" in contract
    assert "never invent, infer, rename or fabricate an id" in contract
    assert owned_ids == [str(item_id)]
