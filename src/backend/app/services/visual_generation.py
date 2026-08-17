from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.repositories.entity_repo import EntityRepository
from app.db.tables import Campaign, MediaAsset, Turn
from app.services.scene_state_service import SceneStateService
from app.services.session_zero_service import SessionZeroService

logger = logging.getLogger(__name__)


class ComfyUIError(RuntimeError):
    pass


@dataclass(frozen=True)
class GeneratedVisual:
    kind: str
    file_path: str
    url: str
    prompt: str
    seed: int
    generated: bool


class Flux2PixelWorkflowBuilder:
    """Build compact ComfyUI API workflows for FLUX.2 Klein + a pixel-art LoRA.

    The graph deliberately uses only ComfyUI core nodes. Reference images are encoded
    with the configured VAE and chained into the positive conditioning through
    ReferenceLatent, which lets the same workflow handle zero, one, or several refs.
    """

    @staticmethod
    def build(
        *,
        prompt: str,
        seed: int,
        width: int,
        height: int,
        reference_names: list[str],
        lora_strength: float,
        filename_prefix: str,
    ) -> dict[str, dict]:
        workflow: dict[str, dict] = {
            "1": {
                "class_type": "UNETLoader",
                "inputs": {
                    "unet_name": settings.IMAGE_DIFFUSION_MODEL,
                    "weight_dtype": "default",
                },
            },
            "2": {
                "class_type": "CLIPLoader",
                "inputs": {
                    "clip_name": settings.IMAGE_TEXT_ENCODER,
                    "type": "flux2",
                    "device": "default",
                },
            },
            "3": {
                "class_type": "VAELoader",
                "inputs": {"vae_name": settings.IMAGE_VAE_MODEL},
            },
        }

        model_ref: list[object] = ["1", 0]
        clip_ref: list[object] = ["2", 0]
        if settings.IMAGE_LORA_MODEL:
            workflow["4"] = {
                "class_type": "LoraLoader",
                "inputs": {
                    "model": model_ref,
                    "clip": clip_ref,
                    "lora_name": settings.IMAGE_LORA_MODEL,
                    "strength_model": lora_strength,
                    "strength_clip": lora_strength,
                },
            }
            model_ref = ["4", 0]
            clip_ref = ["4", 1]

        workflow["5"] = {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": prompt, "clip": clip_ref},
        }
        # FLUX.2 Klein is distilled around guidance 1.0. A zeroed negative branch
        # mirrors the official ComfyUI Klein workflow without inventing a second prompt.
        workflow["6"] = {
            "class_type": "ConditioningZeroOut",
            "inputs": {"conditioning": ["5", 0]},
        }

        positive_ref: list[object] = ["5", 0]
        next_id = 20
        for reference_name in reference_names[: settings.IMAGE_MAX_REFERENCES]:
            load_id = str(next_id)
            encode_id = str(next_id + 1)
            ref_id = str(next_id + 2)
            workflow[load_id] = {
                "class_type": "LoadImage",
                "inputs": {"image": reference_name},
            }
            workflow[encode_id] = {
                "class_type": "VAEEncode",
                "inputs": {"pixels": [load_id, 0], "vae": ["3", 0]},
            }
            workflow[ref_id] = {
                "class_type": "ReferenceLatent",
                "inputs": {
                    "conditioning": positive_ref,
                    "latent": [encode_id, 0],
                },
            }
            positive_ref = [ref_id, 0]
            next_id += 3

        workflow.update(
            {
                "7": {
                    "class_type": "EmptyFlux2LatentImage",
                    "inputs": {"width": width, "height": height, "batch_size": 1},
                },
                "8": {
                    "class_type": "RandomNoise",
                    "inputs": {"noise_seed": seed},
                },
                "9": {
                    "class_type": "CFGGuider",
                    "inputs": {
                        "cfg": 1.0,
                        "model": model_ref,
                        "positive": positive_ref,
                        "negative": ["6", 0],
                    },
                },
                "10": {
                    "class_type": "KSamplerSelect",
                    "inputs": {"sampler_name": "euler"},
                },
                "11": {
                    "class_type": "Flux2Scheduler",
                    "inputs": {
                        "steps": settings.IMAGE_STEPS,
                        "width": width,
                        "height": height,
                    },
                },
                "12": {
                    "class_type": "SamplerCustomAdvanced",
                    "inputs": {
                        "noise": ["8", 0],
                        "guider": ["9", 0],
                        "sampler": ["10", 0],
                        "sigmas": ["11", 0],
                        "latent_image": ["7", 0],
                    },
                },
                "13": {
                    "class_type": "VAEDecode",
                    "inputs": {"samples": ["12", 0], "vae": ["3", 0]},
                },
                "14": {
                    "class_type": "SaveImage",
                    "inputs": {
                        "filename_prefix": filename_prefix,
                        "images": ["13", 0],
                    },
                },
            }
        )
        return workflow


class ComfyUIClient:
    def __init__(self, base_url: str | None = None):
        self.base_url = (base_url or settings.IMAGE_BASE_URL).rstrip("/")

    async def health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                response = await client.get(f"{self.base_url}/system_stats")
                return response.is_success
        except httpx.HTTPError:
            return False

    async def release_ollama_vram(self) -> list[str]:
        """Best-effort unload of locally running Ollama models before a GPU image job.

        PersonalDM uses the same consumer GPU for text and images. Ollama deliberately
        keeps models resident after a response, so without this hand-off an 8 GB card can
        enter ComfyUI with several GB already occupied. If Ollama is absent/cloud-only,
        this is simply a no-op.
        """
        if not settings.IMAGE_RELEASE_OLLAMA_VRAM:
            return []
        try:
            async with httpx.AsyncClient(timeout=4.0) as client:
                running = await client.get("http://127.0.0.1:11434/api/ps")
                if not running.is_success:
                    return []
                names = [
                    str(item.get("name") or item.get("model"))
                    for item in (running.json().get("models") or [])
                    if item.get("name") or item.get("model")
                ]
                for name in names:
                    response = await client.post(
                        "http://127.0.0.1:11434/api/generate",
                        json={"model": name, "keep_alive": 0, "stream": False},
                    )
                    response.raise_for_status()
                return names
        except (httpx.HTTPError, ValueError, TypeError):
            return []

    async def upload_image(self, path: Path, *, prefix: str) -> str:
        if not path.is_file():
            raise ComfyUIError(f"Reference image is missing: {path}")
        requested_subfolder = f"personaldm/{prefix}"
        try:
            async with httpx.AsyncClient(timeout=settings.IMAGE_TIMEOUT_SECONDS) as client:
                with path.open("rb") as handle:
                    response = await client.post(
                        f"{self.base_url}/upload/image",
                        files={"image": (path.name, handle, "image/png")},
                        data={
                            "type": "input",
                            "subfolder": requested_subfolder,
                            "overwrite": "true",
                        },
                    )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ComfyUIError(f"ComfyUI reference upload failed: {exc}") from exc

        name = str(payload.get("name") or path.name)
        subfolder = str(payload.get("subfolder") or requested_subfolder).strip("/\\")
        return f"{subfolder}/{name}" if subfolder else name

    async def generate(self, workflow: dict[str, dict]) -> bytes:
        try:
            async with httpx.AsyncClient(timeout=settings.IMAGE_TIMEOUT_SECONDS) as client:
                queued = await client.post(
                    f"{self.base_url}/prompt",
                    json={"prompt": workflow},
                )
                queued.raise_for_status()
                queue_payload = queued.json()
                prompt_id = queue_payload.get("prompt_id")
                if not prompt_id:
                    node_errors = queue_payload.get("node_errors") or {}
                    raise ComfyUIError(
                        "ComfyUI rejected workflow"
                        + (f": {json.dumps(node_errors, ensure_ascii=False)[:1500]}" if node_errors else "")
                    )

                deadline = asyncio.get_running_loop().time() + settings.IMAGE_TIMEOUT_SECONDS
                while asyncio.get_running_loop().time() < deadline:
                    history_response = await client.get(
                        f"{self.base_url}/history/{prompt_id}"
                    )
                    history_response.raise_for_status()
                    history = history_response.json().get(str(prompt_id))
                    if history:
                        status = history.get("status") or {}
                        if status.get("status_str") == "error":
                            messages = status.get("messages") or []
                            raise ComfyUIError(
                                f"ComfyUI generation failed: {str(messages)[-1800:]}"
                            )
                        image = self._first_output_image(history.get("outputs") or {})
                        if image:
                            rendered = await client.get(
                                f"{self.base_url}/view",
                                params={
                                    "filename": image["filename"],
                                    "subfolder": image.get("subfolder") or "",
                                    "type": image.get("type") or "output",
                                },
                            )
                            rendered.raise_for_status()
                            return rendered.content
                    await asyncio.sleep(0.5)
        except ComfyUIError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise ComfyUIError(f"ComfyUI request failed: {exc}") from exc

        raise ComfyUIError(
            f"ComfyUI generation timed out after {settings.IMAGE_TIMEOUT_SECONDS}s"
        )

    @staticmethod
    def _first_output_image(outputs: dict) -> dict | None:
        for node_output in outputs.values():
            for image in node_output.get("images") or []:
                if image.get("filename"):
                    return image
        return None


class VisualGenerationService:
    PORTRAIT_TYPE = "character_portrait"
    CAMPAIGN_COVER_TYPE = "campaign_cover"
    SCENE_TYPE = "scene_illustration"

    def __init__(self, session: AsyncSession, client: ComfyUIClient | None = None):
        self._session = session
        self._entities = EntityRepository(session)
        self._client = client or ComfyUIClient()

    @property
    def generated_root(self) -> Path:
        return Path(settings.DATA_DIR) / settings.IMAGE_GENERATED_SUBDIR

    async def status(self) -> dict:
        enabled = bool(settings.IMAGE_ENABLED)
        connected = enabled and await self._client.health()
        return {
            "enabled": enabled,
            "connected": connected,
            "provider": "comfyui",
            "base_url": settings.IMAGE_BASE_URL,
            "model": settings.IMAGE_DIFFUSION_MODEL,
            "text_encoder": settings.IMAGE_TEXT_ENCODER,
            "lora": settings.IMAGE_LORA_MODEL,
        }

    def character_portrait_path(self, character_id: UUID) -> Path:
        return self.generated_root / "characters" / str(character_id) / "portrait.png"

    def campaign_cover_path(self, campaign_id: UUID) -> Path:
        return self.generated_root / "campaigns" / str(campaign_id) / "cover.png"

    def scene_path(self, scene_id: UUID) -> Path:
        return self.generated_root / "scenes" / str(scene_id) / "latest.png"

    def public_url(self, path: Path) -> str:
        relative = path.resolve().relative_to(self.generated_root.resolve())
        return "/generated/" + relative.as_posix()

    async def generate_character_portrait(
        self,
        character_id: UUID,
        *,
        force: bool = False,
    ) -> GeneratedVisual:
        self._require_enabled()
        character = await self._entities.get_character(character_id)
        if not character:
            raise ValueError("Character not found")
        target = self.character_portrait_path(character_id)
        if target.is_file() and not force:
            return GeneratedVisual(
                kind=self.PORTRAIT_TYPE,
                file_path=str(target),
                url=self.public_url(target),
                prompt="",
                seed=0,
                generated=False,
            )

        prompt = self._portrait_prompt(character)
        return await self._render(
            campaign_id=character.campaign_id,
            target=target,
            kind=self.PORTRAIT_TYPE,
            prompt=prompt,
            width=512,
            height=640,
            lora_strength=settings.IMAGE_PORTRAIT_LORA_STRENGTH,
            metadata={"character_id": str(character_id)},
        )

    async def generate_campaign_cover(
        self,
        campaign_id: UUID,
        *,
        force: bool = False,
    ) -> GeneratedVisual:
        self._require_enabled()
        campaign = await self._session.get(Campaign, str(campaign_id))
        if not campaign:
            raise ValueError("Campaign not found")
        target = self.campaign_cover_path(campaign_id)
        if target.is_file() and not force:
            return GeneratedVisual(
                kind=self.CAMPAIGN_COVER_TYPE,
                file_path=str(target),
                url=self.public_url(target),
                prompt="",
                seed=0,
                generated=False,
            )

        setup = await SessionZeroService(self._session).get(campaign_id)
        prompt = self._campaign_prompt(campaign, setup)
        return await self._render(
            campaign_id=campaign_id,
            target=target,
            kind=self.CAMPAIGN_COVER_TYPE,
            prompt=prompt,
            width=768,
            height=512,
            lora_strength=settings.IMAGE_COVER_LORA_STRENGTH,
            metadata={"session_zero_completed": setup.status == "completed"},
        )

    async def generate_scene(
        self,
        campaign_id: UUID,
        scene_id: UUID,
        *,
        force: bool = True,
    ) -> GeneratedVisual:
        self._require_enabled()
        target = self.scene_path(scene_id)
        if target.is_file() and not force:
            return GeneratedVisual(
                kind=self.SCENE_TYPE,
                file_path=str(target),
                url=self.public_url(target),
                prompt="",
                seed=0,
                generated=False,
            )

        state = await SceneStateService(self._session).get(campaign_id, scene_id)
        prompt = await self._scene_prompt(campaign_id, scene_id, state)
        references: list[Path] = []
        reference_names: list[str] = []
        for participant_id in state.participant_ids[: settings.IMAGE_MAX_REFERENCES]:
            portrait = self.character_portrait_path(participant_id)
            if portrait.is_file():
                references.append(portrait)
                entity = await self._entities.get_by_id(participant_id)
                reference_names.append(
                    entity.canonical_name if entity else str(participant_id)
                )

        return await self._render(
            campaign_id=campaign_id,
            target=target,
            kind=self.SCENE_TYPE,
            prompt=prompt,
            width=768,
            height=512,
            lora_strength=settings.IMAGE_SCENE_LORA_STRENGTH,
            references=references,
            scene_id=scene_id,
            metadata={
                "participant_ids": [str(value) for value in state.participant_ids],
                "reference_character_names": reference_names,
            },
        )

    async def _render(
        self,
        *,
        campaign_id: UUID,
        target: Path,
        kind: str,
        prompt: str,
        width: int,
        height: int,
        lora_strength: float,
        references: list[Path] | None = None,
        scene_id: UUID | None = None,
        metadata: dict | None = None,
    ) -> GeneratedVisual:
        if not await self._client.health():
            raise ComfyUIError(
                f"ComfyUI is not reachable at {settings.IMAGE_BASE_URL}"
            )

        released_models = await self._client.release_ollama_vram()
        if released_models:
            logger.info(
                "Released Ollama models before image generation: %s",
                ", ".join(released_models),
            )
            # Give the driver a brief moment to return freed allocations before Klein loads.
            await asyncio.sleep(0.25)

        seed = random.randint(0, 2_147_483_647)
        uploaded: list[str] = []
        for index, reference in enumerate(references or []):
            uploaded.append(
                await self._client.upload_image(
                    reference,
                    prefix=f"{kind}-{seed}-{index}",
                )
            )

        workflow = Flux2PixelWorkflowBuilder.build(
            prompt=prompt,
            seed=seed,
            width=width,
            height=height,
            reference_names=uploaded,
            lora_strength=lora_strength,
            filename_prefix=f"personaldm/{kind}",
        )
        image_bytes = await self._client.generate(workflow)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        temporary.write_bytes(image_bytes)
        temporary.replace(target)

        relative_path = target.resolve().relative_to(Path(settings.DATA_DIR).resolve())
        asset_metadata = dict(metadata or {})
        asset_metadata.update(
            {
                "width": width,
                "height": height,
                "lora": settings.IMAGE_LORA_MODEL,
                "lora_strength": lora_strength,
                "reference_count": len(uploaded),
                "text_encoder": settings.IMAGE_TEXT_ENCODER,
                "released_ollama_models": released_models,
            }
        )
        self._session.add(
            MediaAsset(
                campaign_id=str(campaign_id),
                asset_type=kind,
                file_path=relative_path.as_posix(),
                prompt=prompt,
                model_name=settings.IMAGE_DIFFUSION_MODEL,
                seed=seed,
                metadata_json=json.dumps(asset_metadata, ensure_ascii=False),
                scene_id=str(scene_id) if scene_id else None,
            )
        )
        await self._session.flush()
        return GeneratedVisual(
            kind=kind,
            file_path=str(target),
            url=self.public_url(target),
            prompt=prompt,
            seed=seed,
            generated=True,
        )

    async def _scene_prompt(self, campaign_id: UUID, scene_id: UUID, state) -> str:
        recent = (
            await self._session.execute(
                select(Turn)
                .where(
                    Turn.campaign_id == str(campaign_id),
                    Turn.scene_id == str(scene_id),
                    Turn.status == "active",
                    Turn.role.in_(("user", "assistant")),
                )
                .order_by(Turn.created_at.desc())
                .limit(settings.IMAGE_SCENE_HISTORY_TURNS)
            )
        ).scalars().all()
        recent = list(reversed(recent))
        story = "\n".join(
            f"{'PLAYER' if turn.role == 'user' else 'DM'}: {self._compact(turn.content, 900)}"
            for turn in recent
        )
        participants = ", ".join(state.participant_names) or "only the protagonist"
        location = " > ".join(state.location_path) or state.scene_title
        return (
            "pixel art game scene, detailed RPG pixel art, coherent environment, "
            "cinematic composition, crisp deliberate pixels, limited harmonious palette, "
            "no text, no captions, no UI, no watermark.\n"
            f"Scene: {state.scene_title}. Location: {location}. "
            f"Time: {state.world_time_label or 'unspecified'}. "
            f"Mood/conflict: {state.active_conflict or state.scene_goal or 'follow the narrative tone'}.\n"
            f"Physically present characters: {participants}. Reference portraits, when supplied, "
            "represent these same characters and must preserve their recognizable appearance.\n"
            "Depict the current moment implied by the latest story, not an unrelated establishing shot.\n"
            f"LATEST STORY:\n{story or 'No turns yet; depict the established starting scene.'}"
        )

    @staticmethod
    def _portrait_prompt(character) -> str:
        fields = [
            character.description,
            character.appearance,
            character.face_description,
            character.body_description,
            character.immutable_features,
        ]
        visual = "; ".join(
            " ".join(str(value).split()) for value in fields if value
        )
        return (
            "pixel art sprite, RPG character portrait, head and shoulders, centered single character, "
            "expressive readable face, strong silhouette, crisp deliberate pixels, detailed 32-bit era "
            "pixel art, simple thematic background, no text, no caption, no frame, no watermark.\n"
            f"Character: {character.canonical_name}.\n"
            f"Appearance: {visual or 'derive a distinctive appearance from the character description without adding text.'}"
        )

    @staticmethod
    def _campaign_prompt(campaign, setup) -> str:
        return (
            "pixel art game key art, RPG campaign cover illustration, atmospheric wide composition, "
            "crisp deliberate pixels, detailed 32-bit era pixel art, strong focal point, no written "
            "title, no letters, no captions, no UI, no watermark.\n"
            f"Campaign concept: {campaign.name}. "
            f"Setting: {setup.setting_name or setup.genre or campaign.description or 'fantasy adventure'}. "
            f"Premise: {setup.premise or setup.world_summary or campaign.description or 'an unfolding adventure'}. "
            f"Tone: {setup.tone or campaign.narrative_style or 'adventurous'}. "
            f"Starting situation: {setup.starting_situation or 'the beginning of the campaign'}."
        )

    @staticmethod
    def _compact(value: str, limit: int) -> str:
        clean = " ".join(str(value or "").split())
        return clean if len(clean) <= limit else clean[: limit - 1] + "…"

    @staticmethod
    def _require_enabled() -> None:
        if not settings.IMAGE_ENABLED:
            raise ComfyUIError(
                "Local image generation is disabled. Start PersonalDM through play.bat "
                "or set PDM_IMAGE_ENABLED=true."
            )


__all__ = [
    "ComfyUIClient",
    "ComfyUIError",
    "Flux2PixelWorkflowBuilder",
    "GeneratedVisual",
    "VisualGenerationService",
]
