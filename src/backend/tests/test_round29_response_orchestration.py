from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.config import settings
from app.models.provider_config import ProviderConfigRead
from app.services.actor_memory_observability_guard import _augment_trace
from app.services.role_model_router import ModelRole, RoleModelRouter
from app.services.systemless_authority_guard import (
    addressed_response_requested,
    detect_self_repetition,
    input_uses_addressed_character,
    normalize_addressed_response,
)
from app.services.turn_authority_planner import CoordinatedTurnPlan
from app.services.turn_planner import ActionSequencePlan, ActionStepPlan


def _base_plan(**updates) -> CoordinatedTurnPlan:
    payload = {
        "player_intent": "Продолжить текущий ход",
        "resolution": "conversation",
        "observable_consequences": [],
        "character_beats": [],
        "canon_constraints": [],
        "new_fact_candidates": [],
        "narration_guidance": [],
        "ending_hook": "",
    }
    payload.update(updates)
    return CoordinatedTurnPlan.model_validate(payload)


def test_sticky_listener_is_only_input_provenance_not_response_ownership() -> None:
    # The UI-selected listener is deliberately preserved without lexical interpretation. Planner
    # later decides whether that listener actually owns a response on this turn.
    assert input_uses_addressed_character("Выхожу из офиса и направляюсь к месту происшествия.") is True
    assert input_uses_addressed_character("Во сколько это произошло?") is True

    pure_action = _base_plan(addressed_response_requested=False)
    question = _base_plan(
        addressed_response_requested=True,
        response_ownership_reason="Игрок задаёт выбранному NPC прямой вопрос.",
    )
    assert addressed_response_requested("Выхожу из офиса.", pure_action) is False
    assert addressed_response_requested("Во сколько это произошло?", question) is True


def test_conversation_plan_keeps_addressed_response_only_when_planner_requests_it() -> None:
    plan = _base_plan(
        resolution="conversation",
        addressed_response_requested=True,
        response_ownership_reason="Продолжение разговора с выбранным NPC.",
    )
    assert addressed_response_requested("Да, продолжайте.", plan) is True


def test_mixed_action_and_dialogue_is_already_separated_by_typed_plan() -> None:
    plan = _base_plan(
        resolution="sequence",
        addressed_response_requested=True,
        response_ownership_reason="Игрок осматривает фотографии и задаёт вопрос Марине.",
        action_sequence=ActionSequencePlan(
            steps=[
                ActionStepPlan(
                    action_type="interaction",
                    intent="осмотреть лежащие на столе фотографии",
                    resolution="auto_success",
                    safe_mundane=True,
                    observable_outcome="Фотографии осмотрены.",
                )
            ]
        ),
        observable_consequences=["Фотографии осмотрены."],
    )

    normalized = normalize_addressed_response(
        plan,
        "Осматриваю лежащие на столе фотографии. Марина, это вы их сделали?",
    )

    assert normalized is plan
    assert normalized.resolution == "sequence"
    assert len(normalized.action_sequence.steps) == 1
    assert normalized.action_sequence.steps[0].resolution == "auto_success"
    assert normalized.addressed_response_requested is True


def test_requires_check_is_invalid_even_for_addressed_dialogue() -> None:
    with pytest.raises(ValidationError):
        ActionStepPlan(
            action_type="interaction",
            intent="спросить Марину, когда это произошло",
            resolution="requires_check",
        )


def test_repetition_guard_detects_duplicate_inside_same_response() -> None:
    sentence = (
        "Вы внимательно осматриваете дверь, но ничего особенного не замечаете и не находите "
        "никаких странных знаков."
    )
    candidate = f"{sentence} {sentence}"

    match = detect_self_repetition(candidate)

    assert match is not None
    assert match.similarity == 1.0
    assert match.exact is True


@pytest.mark.asyncio
async def test_control_roles_do_not_fall_back_to_campaign_narrator(monkeypatch) -> None:
    campaign_id = uuid4()
    primary = ProviderConfigRead(
        id=uuid4(),
        campaign_id=campaign_id,
        base_url="http://localhost:11434",
        model_name="gemma4:e4b",
        has_api_key=False,
        context_window=6144,
        created_at=datetime.utcnow(),
    )
    repo = SimpleNamespace(
        get_by_campaign_id=AsyncMock(return_value=primary),
        get_decrypted_key=AsyncMock(return_value=None),
    )
    monkeypatch.setattr(settings, "CONTROL_LLM_MODEL", "qwen2.5:7b")
    monkeypatch.setattr(settings, "CONTROL_LLM_BASE_URL", "http://localhost:11434")
    monkeypatch.setattr(settings, "CONTROL_LLM_CONTEXT_WINDOW", 6144)
    monkeypatch.setattr(settings, "CONTROL_LLM_API_KEY", None)

    selection = await RoleModelRouter(repo).resolve(
        campaign_id,
        ModelRole.PLANNER,
        primary,
    )

    assert selection is not None
    assert selection.config.model_name == "qwen2.5:7b"
    assert selection.fallback_config.model_name == "qwen2.5:7b"
    assert selection.has_distinct_fallback is False


def test_actor_memory_dedup_is_not_reported_as_dropout() -> None:
    assistant_id = str(uuid4())
    actor_id = str(uuid4())
    recipient_id = str(uuid4())
    claim = "Я видел красную машину возле старого дома около полуночи."
    audit = {
        "actor_id": actor_id,
        "recipient_id": recipient_id,
        "selector_status": "selected",
        "selector_attempts": 1,
        "selector_error": None,
        "candidate_segments": [{"segment_id": 1, "text": claim}],
        "selected_segment_ids": [1],
        "actor_evidence_knowledge_created": 1,
    }
    snapshot = {
        "turns": [
            {
                "id": assistant_id,
                "actor_id": actor_id,
                "content": claim,
                "context_snapshot": {"actor_memory_debug": audit},
            }
        ],
        "beliefs": [
            {
                "character_id": recipient_id,
                "source_character_id": actor_id,
                "proposition": claim,
                "is_current": True,
            }
        ],
        "proposals": [],
    }
    trace = {
        "memory": {},
        "diagnostics": [
            {
                "code": "ACTOR_MEMORY_DROPOUT",
                "severity": "warning",
                "detail": "old coarse diagnostic",
            }
        ],
    }

    augmented = _augment_trace(snapshot, assistant_id, trace)

    assert not any(
        item["code"] == "ACTOR_MEMORY_DROPOUT"
        for item in augmented["diagnostics"]
    )
    assert augmented["memory"]["actor_selector"] == audit


def test_empty_selector_gets_precise_diagnostic_instead_of_dropout() -> None:
    assistant_id = str(uuid4())
    audit = {
        "actor_id": str(uuid4()),
        "recipient_id": str(uuid4()),
        "selector_status": "empty_selection",
        "selector_attempts": 2,
        "selector_error": None,
        "candidate_segments": [
            {
                "segment_id": 1,
                "text": "Я совершенно точно видел красную машину возле старого дома около полуночи.",
            }
        ],
        "selected_segment_ids": [],
    }
    snapshot = {
        "turns": [
            {
                "id": assistant_id,
                "actor_id": audit["actor_id"],
                "content": audit["candidate_segments"][0]["text"],
                "context_snapshot": {"actor_memory_debug": audit},
            }
        ],
        "beliefs": [],
        "proposals": [],
    }
    trace = {
        "memory": {},
        "diagnostics": [
            {
                "code": "ACTOR_MEMORY_DROPOUT",
                "severity": "warning",
                "detail": "old coarse diagnostic",
            }
        ],
    }

    augmented = _augment_trace(snapshot, assistant_id, trace)
    codes = {item["code"] for item in augmented["diagnostics"]}

    assert "ACTOR_MEMORY_DROPOUT" not in codes
    assert "ACTOR_SELECTOR_EMPTY" in codes
