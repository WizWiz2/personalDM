from datetime import datetime
from uuid import uuid4

import pytest

from app.config import settings
from app.models.provider_config import ProviderConfigRead
from app.models.turn import ChatMessage
from app.providers.llm_provider import LLMProviderError
from app.services import role_model_router as router_module
from app.services.role_model_router import ModelRole, RoleModelRouter, RoleModelSelection


class _SlowControlProvider:
    def __init__(self):
        self.last_telemetry = {}

    async def generate_json(self, *args, **kwargs):
        raise AssertionError("wait_for test double should intercept the request")


def _selection(role: ModelRole) -> RoleModelSelection:
    campaign_id = uuid4()
    config = ProviderConfigRead(
        id=uuid4(),
        campaign_id=campaign_id,
        base_url="http://localhost:11434/v1",
        model_name="test-control",
        has_api_key=False,
        context_window=4096,
        created_at=datetime.utcnow(),
    )
    return RoleModelSelection(
        role=role,
        config=config,
        api_key=None,
        fallback_config=config,
        fallback_api_key=None,
        source="test",
    )


@pytest.mark.asyncio
async def test_control_role_has_one_wall_clock_budget(monkeypatch):
    observed = {}

    async def timeout_immediately(awaitable, *, timeout):
        observed["timeout"] = timeout
        awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(router_module.asyncio, "wait_for", timeout_immediately)
    monkeypatch.setattr(settings, "CONTROL_LLM_TIMEOUT_SECONDS", 37.0)

    provider = _SlowControlProvider()
    router = RoleModelRouter(config_repo=None)

    with pytest.raises(LLMProviderError, match="planner exceeded 37s"):
        await router.generate_json(
            provider,
            _selection(ModelRole.PLANNER),
            [ChatMessage(role="system", content="Return JSON")],
        )

    assert observed["timeout"] == 37.0
    assert provider.last_telemetry["status"] == "control_timeout"
    assert provider.last_telemetry["model_role"] == "planner"
    assert provider.last_telemetry["timeout_seconds"] == 37.0


@pytest.mark.asyncio
async def test_narrator_role_does_not_use_control_timeout(monkeypatch):
    called = False

    async def forbidden_wait_for(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("narrator must not use the structured control timeout")

    class _FastProvider:
        last_telemetry = {}

        async def generate_json(self, *args, **kwargs):
            return {"ok": True}

    monkeypatch.setattr(router_module.asyncio, "wait_for", forbidden_wait_for)
    provider = _FastProvider()
    result = await RoleModelRouter(config_repo=None).generate_json(
        provider,
        _selection(ModelRole.NARRATOR),
        [ChatMessage(role="system", content="Return JSON")],
    )

    assert result == {"ok": True}
    assert called is False
