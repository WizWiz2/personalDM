from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from uuid import UUID

from app.config import settings
from app.db.repositories.provider_config_repo import ProviderConfigRepository
from app.models.provider_config import ProviderConfigRead
from app.models.turn import ChatMessage
from app.providers.llm_provider import LLMProvider, LLMProviderError


class ModelRole(str, Enum):
    NARRATOR = "narrator"
    SCRIBE = "scribe"
    CURATOR = "curator"
    EVALUATOR = "evaluator"
    CHARACTER_BUILDER = "character_builder"
    STRUCTURED_REPAIR = "structured_repair"


CONTROL_ROLES = {
    ModelRole.SCRIBE,
    ModelRole.CURATOR,
    ModelRole.EVALUATOR,
    ModelRole.STRUCTURED_REPAIR,
}


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
            self.config.base_url.rstrip("/") != self.fallback_config.base_url.rstrip("/")
            or self.config.model_name != self.fallback_config.model_name
        )


class RoleModelRouter:
    """Resolve one campaign provider into role-specific model selections.

    The campaign provider remains the source of truth for narration. Control roles can
    override the model and endpoint through process settings, while retaining a safe
    fallback to the campaign provider when the control model is unavailable.
    """

    def __init__(self, config_repo: ProviderConfigRepository):
        self._config_repo = config_repo

    @staticmethod
    def _model_override(role: ModelRole) -> str | None:
        return {
            ModelRole.SCRIBE: settings.SCRIBE_LLM_MODEL,
            ModelRole.CURATOR: settings.CURATOR_LLM_MODEL,
            ModelRole.EVALUATOR: settings.EVALUATOR_LLM_MODEL,
            ModelRole.CHARACTER_BUILDER: settings.CHARACTER_BUILDER_LLM_MODEL,
            ModelRole.STRUCTURED_REPAIR: settings.CONTROL_LLM_MODEL,
        }.get(role)

    async def resolve(
        self,
        campaign_id: UUID,
        role: ModelRole,
        primary_config: ProviderConfigRead | None = None,
    ) -> RoleModelSelection | None:
        primary = primary_config or await self._config_repo.get_by_campaign_id(campaign_id)
        if primary is None:
            return None
        primary_key = await self._config_repo.get_decrypted_key(campaign_id)

        explicit_model = self._model_override(role)
        if role == ModelRole.NARRATOR or (
            role == ModelRole.CHARACTER_BUILDER and not explicit_model
        ):
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

        model_name = explicit_model or settings.CONTROL_LLM_MODEL or primary.model_name
        base_url = settings.CONTROL_LLM_BASE_URL or primary.base_url
        context_window = settings.CONTROL_LLM_CONTEXT_WINDOW or primary.context_window
        if settings.CONTROL_LLM_API_KEY is not None:
            api_key = settings.CONTROL_LLM_API_KEY
        elif base_url.rstrip("/") == primary.base_url.rstrip("/"):
            api_key = primary_key
        else:
            # Never forward a campaign secret to a different endpoint implicitly.
            api_key = None

        source = "role_override" if explicit_model else "control_default"
        return RoleModelSelection(
            role=role,
            config=primary.model_copy(
                update={
                    "base_url": base_url,
                    "model_name": model_name,
                    "context_window": context_window,
                    "has_api_key": bool(api_key),
                }
            ),
            api_key=api_key,
            fallback_config=primary,
            fallback_api_key=primary_key,
            source=source,
        )

    async def generate_json(
        self,
        provider: LLMProvider,
        selection: RoleModelSelection,
        messages: list[ChatMessage],
        **kwargs,
    ) -> dict:
        try:
            result = await provider.generate_json(
                messages,
                selection.config,
                selection.api_key,
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
            result = await provider.generate_json(
                messages,
                selection.fallback_config,
                selection.fallback_api_key,
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
