from __future__ import annotations

import base64

import httpx

from app.config import settings


class CloudImageProviderError(RuntimeError):
    pass


class OpenAIImageClient:
    """Duck-typed replacement for ComfyUIClient used by VisualGenerationService.

    The existing visual pipeline hands the client a workflow dict. For cloud mode we
    extract the prompt and dimensions from that graph and call an OpenAI-compatible
    `/images/generations` endpoint. Reference uploads are intentionally ignored for the
    first cloud implementation; scene/portrait prompts still carry canonical appearance.
    """

    def __init__(self, base_url: str | None = None, model: str | None = None, api_key: str | None = None):
        self.base_url = (base_url or settings.IMAGE_CLOUD_BASE_URL).rstrip("/")
        self.model = model or settings.IMAGE_CLOUD_MODEL
        self.api_key = api_key if api_key is not None else settings.IMAGE_API_KEY

    async def health(self) -> bool:
        if not self.api_key:
            return False
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(
                    f"{self.base_url}/models",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
                return response.is_success
        except httpx.HTTPError:
            return False

    async def release_ollama_vram(self) -> list[str]:
        return []

    async def upload_image(self, path, *, prefix: str) -> str:
        # Keep the same client contract. The first cloud backend is text-to-image only;
        # VisualGenerationService may still enumerate local references for local mode.
        return f"ignored/{prefix}/{getattr(path, 'name', 'reference.png')}"

    async def generate(self, workflow: dict[str, dict]) -> bytes:
        if not self.api_key:
            raise CloudImageProviderError("Cloud image API key is not configured")
        prompt = str((workflow.get("5") or {}).get("inputs", {}).get("text") or "")
        latent = (workflow.get("7") or {}).get("inputs", {})
        width = int(latent.get("width") or 1024)
        height = int(latent.get("height") or 1024)
        size = self._size(width, height)
        try:
            async with httpx.AsyncClient(timeout=settings.IMAGE_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    f"{self.base_url}/images/generations",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "prompt": prompt,
                        "size": size,
                    },
                )
                response.raise_for_status()
                payload = response.json()
                item = (payload.get("data") or [None])[0] or {}
                encoded = item.get("b64_json")
                if encoded:
                    return base64.b64decode(encoded)
                url = item.get("url")
                if url:
                    rendered = await client.get(url)
                    rendered.raise_for_status()
                    return rendered.content
                raise CloudImageProviderError("Cloud image API returned no image data")
        except CloudImageProviderError:
            raise
        except (httpx.HTTPError, ValueError, TypeError, base64.binascii.Error) as exc:
            raise CloudImageProviderError(f"Cloud image generation failed: {exc}") from exc

    @staticmethod
    def _size(width: int, height: int) -> str:
        if width > height * 1.15:
            return "1536x1024"
        if height > width * 1.15:
            return "1024x1536"
        return "1024x1024"


__all__ = ["CloudImageProviderError", "OpenAIImageClient"]
