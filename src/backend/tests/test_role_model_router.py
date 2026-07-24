from datetime import datetime
from uuid import uuid4

import pytest

from app.config import settings
from app.models.provider_config import ProviderConfigRead
from app.services.post_turn_processor import should_run_periodic_job
from app.services.role_model_router import ModelRole, RoleModelRouter


class FakeConfigRepo:
    def __init__(self, config, api_key="campaign-secret"):
        self.config = config
        self.api_key = api_key

    async def get_by_campaign_id(self, campaign_id):
        return self.config

    async def get_decrypted_key(self, campaign_id):
        return self.api_key


def primary_config():
    campaign_id = uuid4()
    return ProviderConfigRead(
        id=uuid4(),
        campaign_id=campaign_id,
        base_url="http://localhost:11434/v1",
        model_name="gemma4:e4b",
        has_api_key=True,
        context_window=6144,
        created_at=datetime.utcnow(),
    )


@pytest.mark.asyncio
async def test_narrator_and_builder_keep_campaign_model(monkeypatch):
    primary = primary_config()
    monkeypatch.setattr(settings, "CHARACTER_BUILDER_LLM_MODEL", None)
    router = RoleModelRouter(FakeConfigRepo(primary))

    narrator = await router.resolve(primary.campaign_id, ModelRole.NARRATOR)
    builder = await router.resolve(primary.campaign_id, ModelRole.CHARACTER_BUILDER)

    assert narrator.config.model_name == "gemma4:e4b"
    assert builder.config.model_name == "gemma4:e4b"
    assert narrator.source == builder.source == "campaign_primary"


@pytest.mark.asyncio
async def test_control_roles_default_to_qwen_on_same_local_endpoint(monkeypatch):
    primary = primary_config()
    monkeypatch.setattr(settings, "CONTROL_LLM_MODEL", "qwen2.5:7b")
    monkeypatch.setattr(settings, "CONTROL_LLM_BASE_URL", None)
    monkeypatch.setattr(settings, "CONTROL_LLM_API_KEY", None)
    monkeypatch.setattr(settings, "SCRIBE_LLM_MODEL", None)
    router = RoleModelRouter(FakeConfigRepo(primary))

    selection = await router.resolve(primary.campaign_id, ModelRole.SCRIBE)

    assert selection.config.model_name == "qwen2.5:7b"
    assert selection.config.base_url == primary.base_url
    assert selection.api_key == "campaign-secret"
    assert selection.has_distinct_fallback is True


@pytest.mark.asyncio
async def test_control_endpoint_does_not_receive_campaign_secret_implicitly(monkeypatch):
    primary = primary_config()
    monkeypatch.setattr(settings, "CONTROL_LLM_BASE_URL", "https://control.example/v1")
    monkeypatch.setattr(settings, "CONTROL_LLM_API_KEY", None)
    router = RoleModelRouter(FakeConfigRepo(primary))

    selection = await router.resolve(primary.campaign_id, ModelRole.CURATOR)

    assert selection.config.base_url == "https://control.example/v1"
    assert selection.api_key is None
    assert selection.fallback_api_key == "campaign-secret"


@pytest.mark.asyncio
async def test_per_role_model_override_wins(monkeypatch):
    primary = primary_config()
    monkeypatch.setattr(settings, "EVALUATOR_LLM_MODEL", "qwen-evaluator:test")
    router = RoleModelRouter(FakeConfigRepo(primary))

    selection = await router.resolve(primary.campaign_id, ModelRole.EVALUATOR)

    assert selection.config.model_name == "qwen-evaluator:test"
    assert selection.source == "role_override"


def test_periodic_job_runs_first_turn_and_then_on_interval():
    assert should_run_periodic_job(1, 3) is True
    assert should_run_periodic_job(2, 3) is False
    assert should_run_periodic_job(3, 3) is True
    assert should_run_periodic_job(4, 1) is True
