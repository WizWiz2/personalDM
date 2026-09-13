"""Cloud image client: text-only vs reference (edits) routing."""
from __future__ import annotations

import base64
from pathlib import Path

import httpx
import pytest

from app.providers.image_provider import OpenAIImageClient
from app.services.visual_generation import ComfyUIError, Flux2PixelWorkflowBuilder


TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _workflow(prompt: str = "pixel art portrait of a ranger", width: int = 1024, height: int = 1024):
    return Flux2PixelWorkflowBuilder.build(
        prompt=prompt,
        seed=7,
        width=width,
        height=height,
        reference_names=[],
        lora_strength=1.0,
        filename_prefix="personaldm/test",
    )


def _patch_transport(monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport):
    original = httpx.AsyncClient

    class TrackingClient(original):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", TrackingClient)


@pytest.mark.asyncio
async def test_generate_without_refs_uses_images_generations(monkeypatch: pytest.MonkeyPatch):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["content_type"] = request.headers.get("content-type", "")
        seen["body"] = request.read().decode("utf-8")
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(TINY_PNG).decode("ascii")}]},
        )

    _patch_transport(monkeypatch, httpx.MockTransport(handler))
    client = OpenAIImageClient(
        base_url="https://example.test/v1",
        model="gpt-image-2",
        api_key="sk-test",
    )
    out = await client.generate(_workflow())
    assert out == TINY_PNG
    assert seen["path"].endswith("/images/generations")
    assert "application/json" in seen["content_type"]
    assert "gpt-image-2" in seen["body"]
    assert "pixel art portrait" in seen["body"]


@pytest.mark.asyncio
async def test_generate_with_refs_uses_images_edits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ref_a = tmp_path / "hero.png"
    ref_b = tmp_path / "npc.png"
    ref_a.write_bytes(TINY_PNG)
    ref_b.write_bytes(TINY_PNG)
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["content_type"] = request.headers.get("content-type", "")
        seen["body"] = request.read()
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(TINY_PNG).decode("ascii")}]},
        )

    _patch_transport(monkeypatch, httpx.MockTransport(handler))
    client = OpenAIImageClient(
        base_url="https://example.test/v1",
        model="gpt-image-2",
        api_key="sk-test",
    )
    token_a = await client.upload_image(ref_a, prefix="portrait-1-0")
    token_b = await client.upload_image(ref_b, prefix="portrait-1-1")
    assert token_a.startswith("cloud-ref/")
    assert token_b.startswith("cloud-ref/")

    out = await client.generate(_workflow(width=768, height=512))
    assert out == TINY_PNG
    assert seen["path"].endswith("/images/edits")
    assert "multipart/form-data" in seen["content_type"]
    body = seen["body"]
    assert b"hero.png" in body
    assert b"npc.png" in body
    assert b'name="prompt"' in body or b"name=prompt" in body
    assert b"gpt-image-2" in body
    assert client._pending_references == []


@pytest.mark.asyncio
async def test_upload_missing_reference_raises(tmp_path: Path):
    client = OpenAIImageClient(api_key="sk-test")
    with pytest.raises(ComfyUIError, match="missing"):
        await client.upload_image(tmp_path / "nope.png", prefix="x")
