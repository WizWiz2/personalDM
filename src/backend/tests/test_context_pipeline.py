from __future__ import annotations

from uuid import uuid4

import pytest

from app.models.turn import ChatMessage
from app.services.context_pipeline import (
    CompiledContext,
    ContextPipeline,
    ContextRequest,
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
