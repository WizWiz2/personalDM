import json
from types import SimpleNamespace

import pytest

from app.models.turn import ChatMessage
from app.providers.llm_provider import LLMProvider
from app.services.turn_runner import TurnRunner
from tests import run_realistic_simulation_v2 as runtime
from tests import simulation_quality_controls as quality


class MockConfig:
    base_url = "http://127.0.0.1:11434/v1"
    model_name = "mock"


class MockProvider(LLMProvider):
    async def generate_stream(self, messages, config, api_key=None, **kwargs):
        self.last_telemetry = {"status": "completed", "mock": True}
        yield "```json\n{\"ok\": true, \"value\": 7}\n```"


def test_balanced_json_parser_handles_fenced_content():
    assert quality._balanced_json_object('prefix {"answer":{"value":1}} suffix') == {
        "answer": {"value": 1}
    }


@pytest.mark.asyncio
async def test_control_json_uses_mock_stream_without_http():
    result = await quality.generate_control_json(
        MockProvider(),
        [ChatMessage(role="system", content="Return JSON")],
        MockConfig(),
        None,
        label="unit_control",
        max_tokens=100,
        temperature=0.0,
    )
    assert result is not None
    assert result.data == {"ok": True, "value": 7}
    assert result.transport == "mock_stream"


def test_current_user_message_is_reserved_by_removing_old_history():
    messages = [
        ChatMessage(role="system", content="system " * 30),
        ChatMessage(role="user", content="old question " * 20),
        ChatMessage(role="assistant", content="old answer " * 20),
    ]
    updated, metadata = quality.ensure_current_user_message(
        messages,
        {"token_budget_max": 80, "included_layers": ["layer_0_system"]},
        "current question",
    )
    assert updated[-1].role == "user"
    assert updated[-1].content == "current question"
    assert metadata["current_user_reserved"] is True
    assert metadata["history_messages_removed_for_current_user"] >= 1


def test_turn_runner_reserves_current_user_in_production_path():
    messages = [
        ChatMessage(role="system", content="system " * 30),
        ChatMessage(role="user", content="old question " * 20),
        ChatMessage(role="assistant", content="old answer " * 20),
    ]
    updated, metadata = TurnRunner._reserve_current_user(
        messages,
        {"token_budget_max": 80, "included_layers": ["layer_0_system"]},
        "current question",
    )
    assert updated[-1] == ChatMessage(role="user", content="current question")
    assert metadata["current_user_reserved"] is True
    assert metadata["history_messages_removed_for_current_user"] >= 1


def test_smoke_fallback_discards_dm_only_thesis(monkeypatch):
    monkeypatch.setenv("PDM_SIM_MODE", "smoke")
    quality.install_quality_controls(runtime)
    policy = runtime.PlayerPolicy()
    decision = policy.fallback(
        ["Sylvia"],
        "question",
        "Найти безопасный путь",
        "ДМ подтвердил след у ворот.",
        ["СЕКРЕТ: Сильвия украла запретную книгу"],
        3,
    )
    assert "украла запретную книгу" not in decision.intent.casefold()


def test_resume_parser_preserves_target_and_intent():
    from tests.run_realistic_simulation import _decision_from_user_content

    decision = _decision_from_user_content(
        "[/talk Garrick] Я спрашиваю Гаррика, какой путь безопаснее?"
    )
    assert decision is not None
    assert decision.target == "Garrick"
    assert decision.mode == "question"
    assert decision.intent == "Я спрашиваю Гаррика, какой путь безопаснее?"


def test_health_snapshot_is_json_serializable():
    json.dumps(quality.health_snapshot(), ensure_ascii=False)


def test_evaluator_history_drops_duplicate_current_assistant_result():
    history = [
        SimpleNamespace(role="user", content="Проверяю дверь"),
        SimpleNamespace(role="assistant", content="Дверь открылась"),
    ]
    selected = quality.evaluator_history_without_duplicate(
        history,
        "Дверь открылась",
    )
    assert [item.content for item in selected] == ["Проверяю дверь"]


def test_player_decision_schema_rejects_unknown_mode():
    with pytest.raises(Exception):
        quality.PlayerDecisionPayload.model_validate(
            {"target": "narrator", "mode": "observe_forever", "intent": "Смотрю"}
        )
