from __future__ import annotations

import base64
from pathlib import Path

import httpx

from app.config import settings


def _visual_error(message: str, cause: Exception | None = None):
    # Lazy import avoids a module cycle: visual_generation imports this client through
    # visual_provider_factory, while the shared API already understands ComfyUIError.
    from app.services.visual_generation import ComfyUIError

    error = ComfyUIError(message)
    if cause is None:
        return error
    return error.with_traceback(cause.__traceback__)


class OpenAIImageClient:
    """Duck-typed replacement for ComfyUIClient used by VisualGenerationService.

    The visual pipeline still builds a Comfy workflow dict. Cloud mode extracts the
    prompt and dimensions from that graph. Local reference files registered via
    `upload_image` are sent to OpenAI-compatible `/images/edits` so portrait/scene
    consistency can use the same refs as Comfy. Without refs we keep text-only
    `/images/generations`.
    """

    # OpenAI GPT Image edits accept multiple reference images; keep a hard ceiling
    # slightly above our local IMAGE_MAX_REFERENCES default.
    _MAX_CLOUD_REFERENCES = 10

    def __init__(self, base_url: str | None = None, model: str | None = None, api_key: str | None = None):
        self.base_url = (base_url or settings.IMAGE_CLOUD_BASE_URL).rstrip("/")
        self.model = model or settings.IMAGE_CLOUD_MODEL
        self.api_key = api_key if api_key is not None else settings.IMAGE_API_KEY
        self._pending_references: list[Path] = []

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
        """Queue a local reference file for the next generate() call.

        Returns a stable token name so VisualGenerationService can keep building the
        Comfy-shaped workflow; cloud generation reads the queued Paths, not the token.
        """
        reference = Path(path)
        if not reference.is_file():
            raise _visual_error(f"Reference image is missing: {reference}")
        if len(self._pending_references) >= self._MAX_CLOUD_REFERENCES:
            return f"ignored/{prefix}/{reference.name}"
        self._pending_references.append(reference)
        return f"cloud-ref/{prefix}/{reference.name}"

    async def generate(self, workflow: dict[str, dict]) -> bytes:
        if not self.api_key:
            raise _visual_error("Cloud image API key is not configured")
        prompt = str((workflow.get("5") or {}).get("inputs", {}).get("text") or "")
        latent = (workflow.get("7") or {}).get("inputs", {})
        width = int(latent.get("width") or 1024)
        height = int(latent.get("height") or 1024)
        size = self._size(width, height)
        references = list(self._pending_references)
        self._pending_references.clear()
        try:
            if references:
                return await self._generate_with_references(prompt=prompt, size=size, references=references)
            return await self._generate_text_only(prompt=prompt, size=size)
        except Exception as exc:
            from app.services.visual_generation import ComfyUIError

            if isinstance(exc, ComfyUIError):
                raise
            if isinstance(exc, (httpx.HTTPError, ValueError, TypeError, base64.binascii.Error, OSError)):
                raise _visual_error(f"Cloud image generation failed: {exc}", exc) from exc
            raise

    async def _generate_text_only(self, *, prompt: str, size: str) -> bytes:
        async with httpx.AsyncClient(timeout=settings.IMAGE_TIMEOUT_SECONDS) as client:
            response = await client.post(
                f"{self.base_url}/images/generations",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={"model": self.model, "prompt": prompt, "size": size},
            )
            response.raise_for_status()
            return await self._decode_image_payload(client, response.json())

    async def _generate_with_references(
        self,
        *,
        prompt: str,
        size: str,
        references: list[Path],
    ) -> bytes:
        # GPT Image reference synthesis uses the edits endpoint with one or more images.
        files: list[tuple[str, tuple[str, bytes, str]]] = []
        for reference in references:
            files.append(
                (
                    "image[]",
                    (
                        reference.name,
                        reference.read_bytes(),
                        self._mime_type(reference),
                    ),
                )
            )
        data = {
            "model": self.model,
            "prompt": prompt,
            "size": size,
        }
        async with httpx.AsyncClient(timeout=settings.IMAGE_TIMEOUT_SECONDS) as client:
            response = await client.post(
                f"{self.base_url}/images/edits",
                headers={"Authorization": f"Bearer {self.api_key}"},
                data=data,
                files=files,
            )
            response.raise_for_status()
            return await self._decode_image_payload(client, response.json())

    async def _decode_image_payload(self, client: httpx.AsyncClient, payload: dict) -> bytes:
        item = (payload.get("data") or [None])[0] or {}
        encoded = item.get("b64_json")
        if encoded:
            return base64.b64decode(encoded)
        url = item.get("url")
        if url:
            rendered = await client.get(url)
            rendered.raise_for_status()
            return rendered.content
        raise _visual_error("Cloud image API returned no image data")

    @staticmethod
    def _mime_type(path: Path) -> str:
        suffix = path.suffix.lower()
        if suffix in {".jpg", ".jpeg"}:
            return "image/jpeg"
        if suffix == ".webp":
            return "image/webp"
        return "image/png"

    @staticmethod
    def _size(width: int, height: int) -> str:
        if width > height * 1.15:
            return "1536x1024"
        if height > width * 1.15:
            return "1024x1536"
        return "1024x1024"


__all__ = ["OpenAIImageClient"]
