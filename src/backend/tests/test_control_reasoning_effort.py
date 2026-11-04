from types import SimpleNamespace

import pytest

from app.config import settings
from app.providers.llm_provider import LLMProvider
from app.services.role_model_router import ModelRole, RoleModelRouter
from tests.test_role_model_router import FakeConfigRepo, primary_config


@pytest.mark.asyncio
async def test_control_roles_carry_their_own_reasoning_effort_and_narrator_keeps_the_campaign_one(monkeypatch):
    primary = primary_config(base_url="https://cloud.example/v1", model_name="luna")
    monkeypatch.setattr(settings, "LLM_REASONING_EFFORT", "low")
    monkeypatch.setattr(settings, "CONTROL_LLM_REASONING_EFFORT", "none")
    monkeypatch.setattr(settings, "CONTROL_LLM_BASE_URL", None)
    monkeypatch.setattr(settings, "PLANNER_LLM_MODEL", None)
    monkeypatch.setattr(settings, "NARRATION_VALIDATOR_LLM_MODEL", None)
    router = RoleModelRouter(FakeConfigRepo(primary))
    messages = [{"role": "user", "content": "x"}]

    for role in (ModelRole.PLANNER, ModelRole.NARRATION_VALIDATOR):
        selection = await router.resolve(primary.campaign_id, role)
        assert LLMProvider._responses_payload(selection.config, messages)["reasoning"] == {"effort": "none"}
    narrator = await router.resolve(primary.campaign_id, ModelRole.NARRATOR)
    assert LLMProvider._responses_payload(narrator.config, messages)["reasoning"] == {"effort": "low"}
    assert LLMProvider._responses_payload(SimpleNamespace(model_name="m"), messages)["reasoning"] == {"effort": "low"}
