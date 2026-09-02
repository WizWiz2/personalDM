from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlparse
from uuid import UUID

from app.config import settings
from app.db.repositories.provider_config_repo import ProviderConfigRepository
from app.models.provider_config import ProviderConfigRead
from app.models.turn import ChatMessage
from app.providers.llm_provider import LLMProvider, LLMProviderError


class ModelRole(str, Enum):
    NARRATOR = "narrator"
    GAME_MASTER = "game_master"
    SESSION_ZERO = "session_zero"
    PLANNER = "planner"
    NARRATION_VALIDATOR = "narration_validator"
    ENTITY_REGISTRAR = "entity_registrar"
    SCRIBE = "scribe"
    CURATOR = "curator"
    EVALUATOR = "evaluator"
    PLAYER = "player"
    SCENARIO_BUILDER = "scenario_builder"
    CHARACTER_BUILDER = "character_builder"
    STRUCTURED_REPAIR = "structured_repair"


CONTROL_ROLES = {
    ModelRole.PLANNER,
    ModelRole.NARRATION_VALIDATOR,
    ModelRole.ENTITY_REGISTRAR,
    ModelRole.SCRIBE,
    ModelRole.CURATOR,
    ModelRole.EVALUATOR,
    ModelRole.PLAYER,
    ModelRole.SCENARIO_BUILDER,
    ModelRole.STRUCTURED_REPAIR,
}

DEFAULT_LOCAL_CONTROL_MODEL = "qwen2.5:7b"


@dataclass(frozen=True)
class RoleModelSelection:
    role: ModelRole
    config: ProviderConfigRead
    api_key: str | None
    fallback_config: ProviderConfigRead
    fallback_api_key: str | None
    source: str

    @property
    def has_distinct_fallback(self) -> bool:
        return (
            self.config.base_url.rstrip("/")
            != self.fallback_config.base_url.rstrip("/")
            or self.config.model_name != self.fallback_config.model_name
        )


class RoleModelRouter:
    """Resolve one campaign provider into role-specific model selections.

    Narration, session zero, and direct out-of-character game-master dialogue use
    the campaign's primary model. A local Ollama campaign keeps the intentional
    narrator/control split (Gemma plus Qwen by default). A remote/cloud campaign
    uses its selected campaign model for control roles unless a separate control
    endpoint or per-role model is explicitly configured. Structured control never
    silently falls back to a different narrator model after a schema failure.
    """

    def __init__(self, config_repo: ProviderConfigRepository):
        self._config_repo = config_repo

    @staticmethod
    def _model_override(role: ModelRole) -> str | None:
        return {
            ModelRole.PLANNER: settings.PLANNER_LLM_MODEL,
            ModelRole.NARRATION_VALIDATOR: settings.NARRATION_VALIDATOR_LLM_MODEL,
            ModelRole.SCRIBE: settings.SCRIBE_LLM_MODEL,
            ModelRole.CURATOR: settings.CURATOR_LLM_MODEL,
            ModelRole.EVALUATOR: settings.EVALUATOR_LLM_MODEL,
            ModelRole.PLAYER: settings.PLAYER_LLM_MODEL,
            ModelRole.SCENARIO_BUILDER: settings.SCENARIO_BUILDER_LLM_MODEL,
            ModelRole.CHARACTER_BUILDER: settings.CHARACTER_BUILDER_LLM_MODEL,
        }.get(role)

    @staticmethod
    def _is_local_ollama(base_url: str) -> bool:
        try:
            parsed = urlparse(base_url)
            return parsed.hostname in {"localhost", "127.0.0.1", "::1"} and (
                parsed.port in {None, 11434}
            )
        except ValueError:
            return False

    @classmethod
    def _control_model(
        cls,
        primary: ProviderConfigRead,
        explicit_model: str | None,
    ) -> tuple[str, str]:
        if explicit_model:
            return explicit_model, "role_override"

        # A distinct control endpoint is an explicit advanced configuration. Its
        # configured model is therefore authoritative even for a cloud campaign.
        if settings.CONTROL_LLM_BASE_URL:
            return settings.CONTROL_LLM_MODEL or primary.model_name, "control_default"

        # Remote OpenAI-compatible providers generally expose their own model IDs.
        # Sending the local default name (qwen2.5:7b) to that endpoint is invalid.
        # Use the campaign-selected model for Planner/Validator/Scribe as well.
        if not cls._is_local_ollama(primary.base_url):
            return primary.model_name, "campaign_primary_control"

        # RuntimeProviderService historically persisted the narrator model into
        # PDM_CONTROL_LLM_MODEL when saving local settings. Treat that legacy
        # same-model value as unset so the intended local split survives restart.
        configured = settings.CONTROL_LLM_MODEL
        if configured and configured != primary.model_name:
            return configured, "control_default"
        return DEFAULT_LOCAL_CONTROL_MODEL, "local_control_default"

    async def resolve(
        self,
        campaign_id: UUID,
        role: ModelRole,
        primary_config: ProviderConfigRead | None = None,
    ) -> RoleModelSelection | None:
        primary = primary_config or await self._config_repo.get_by_campaign_id(
            campaign_id
        )
        if primary is None:
            return None
        primary_key = await self._config_repo.get_decrypted_key(campaign_id)

        explicit_model = self._model_override(role)
        if role in {
            ModelRole.NARRATOR,
            ModelRole.GAME_MASTER,
            ModelRole.SESSION_ZERO,
        } or (role == ModelRole.CHARACTER_BUILDER and not explicit_model):
            return RoleModelSelection(
                role=role,
                config=primary,
                api_key=primary_key,
                fallback_config=primary,
                fallback_api_key=primary_key,
                source="campaign_primary",
            )

        use_control_defaults = role in CONTROL_ROLES or bool(explicit_model)
        if not use_control_defaults:
            return RoleModelSelection(
                role=role,
                config=primary,
                api_key=primary_key,
                fallback_config=primary,
                fallback_api_key=primary_key,
                source="campaign_primary",
            )

        model_name, source = self._control_model(primary, explicit_model)
        base_url = settings.CONTROL_LLM_BASE_URL or primary.base_url
        context_window = (
            settings.CONTROL_LLM_CONTEXT_WINDOW or primary.context_window
        )
        if settings.CONTROL_LLM_API_KEY is not None:
            api_key = settings.CONTROL_LLM_API_KEY
        elif base_url.rstrip("/") == primary.base_url.rstrip("/"):
            api_key = primary_key
        else:
            api_key = None

        resolved = primary.model_copy(
            update={
                "base_url": base_url,
                "model_name": model_name,
                "context_window": context_window,
                "has_api_key": bool(api_key),
            }
        )
        strict_control = role in CONTROL_ROLES
        return RoleModelSelection(
            role=role,
            config=resolved,
            api_key=api_key,
            # Control-plane correctness depends on a stable role selection. If the
            # selected control model cannot produce a usable structured response,
            # bubble that failure instead of changing models mid-turn.
            fallback_config=resolved if strict_control else primary,
            fallback_api_key=api_key if strict_control else primary_key,
            source=source,
        )

    async def _generate_json_once(
        self,
        provider: LLMProvider,
        selection: RoleModelSelection,
        config: ProviderConfigRead,
        api_key: str | None,
        messages: list[ChatMessage],
        **kwargs,
    ) -> dict:
        request = provider.generate_json(
            messages,
            config,
            api_key,
            **kwargs,
        )
        if selection.role not in CONTROL_ROLES:
            return await request

        timeout_seconds = max(1.0, float(settings.CONTROL_LLM_TIMEOUT_SECONDS))
        try:
            return await asyncio.wait_for(request, timeout=timeout_seconds)
        except TimeoutError as exc:
            telemetry = dict(provider.last_telemetry or {})
            provider.last_telemetry = {
                **telemetry,
                "status": "control_timeout",
                "control_plane": True,
                "model": config.model_name,
                "model_role": selection.role.value,
                "timeout_seconds": timeout_seconds,
            }
            raise LLMProviderError(
                f"Control model role {selection.role.value} exceeded "
                f"{timeout_seconds:g}s request budget"
            ) from exc

    async def generate_json(
        self,
        provider: LLMProvider,
        selection: RoleModelSelection,
        messages: list[ChatMessage],
        **kwargs,
    ) -> dict:
        try:
            result = await self._generate_json_once(
                provider,
                selection,
                selection.config,
                selection.api_key,
                messages,
                **kwargs,
            )
            telemetry = dict(provider.last_telemetry or {})
            telemetry.update(
                {
                    "model_role": selection.role.value,
                    "role_model_source": selection.source,
                    "role_router_fallback": False,
                }
            )
            provider.last_telemetry = telemetry
            return result
        except LLMProviderError as primary_error:
            if not selection.has_distinct_fallback:
                raise
            result = await self._generate_json_once(
                provider,
                selection,
                selection.fallback_config,
                selection.fallback_api_key,
                messages,
                **kwargs,
            )
            telemetry = dict(provider.last_telemetry or {})
            telemetry.update(
                {
                    "model_role": selection.role.value,
                    "role_model_source": selection.source,
                    "role_router_fallback": True,
                    "requested_role_model": selection.config.model_name,
                    "role_model_error": str(primary_error)[:1200],
                }
            )
            provider.last_telemetry = telemetry
            return result
