from __future__ import annotations

import pytest

from app.models.player_intent import PlayerIntentContract
from app.services.turn_outcome_resolver import (
    TurnOutcomeDecisionDraft,
    _outcome_wire_model,
    normalize_outcome_draft,
    stamp_world_state_answer,
)
from app.services.turn_planner import TurnPlanningError


def _movement_contract() -> PlayerIntentContract:
    return PlayerIntentContract.model_validate(
        {
            "summary": "Кай идёт в коридор.",
            "actions": [
                {
                    "action_type": "movement",
                    "intent": "Выйти в коридор.",
                    "destination_location": "Коридор",
                }
            ],
        }
    )


def test_nonblocked_outcome_discards_stray_blocking_reason() -> None:
    draft = TurnOutcomeDecisionDraft.model_validate(
        {
            "action_outcomes": [
                {
                    "action_index": 0,
                    "resolution": "auto_success",
                    "safe_mundane": True,
                    "observable_outcome": "Кай выходит в коридор.",
                    "blocking_reason": "none",
                }
            ]
        }
    )

    decision = normalize_outcome_draft(draft, _movement_contract())

    assert decision.action_outcomes[0].blocking_reason is None
    assert decision.action_outcomes[0].safe_mundane is True


def test_stable_npc_without_identity_evidence_is_downgraded_to_temporary_role() -> None:
    draft = TurnOutcomeDecisionDraft.model_validate(
        {
            "action_outcomes": [
                {
                    "action_index": 0,
                    "resolution": "auto_success",
                    "observable_outcome": "Кай встречает дежурного.",
                }
            ],
            "npc_introductions": [
                {
                    "canonical_name": "Роэн",
                    "role": "дежурный",
                    "description": "Дежурный в тёмной рабочей куртке спокойно стоит у входа в помещение.",
                    "appearance": "Короткие волосы, простая одежда и связка служебных ключей на поясе.",
                    "temporary_name": False,
                    "personal_name_evidence": None,
                    "reason": "Игрок непосредственно встречает дежурного у входа.",
                }
            ],
        }
    )

    decision = normalize_outcome_draft(draft, _movement_contract())

    npc = decision.npc_introductions[0]
    assert npc.temporary_name is True
    assert npc.personal_name_evidence is None


def test_unbacked_complication_is_disabled_instead_of_failing_schema() -> None:
    draft = TurnOutcomeDecisionDraft.model_validate(
        {
            "action_outcomes": [
                {"action_index": 0, "resolution": "auto_success"}
            ],
            "allow_new_complication": True,
            "complication_source": None,
        }
    )

    decision = normalize_outcome_draft(draft, _movement_contract())

    assert decision.allow_new_complication is False
    assert decision.complication_source is None


def test_missing_frozen_action_outcome_still_fails_closed() -> None:
    draft = TurnOutcomeDecisionDraft.model_validate({"action_outcomes": []})

    with pytest.raises(TurnPlanningError, match="did not preserve frozen action coverage"):
        normalize_outcome_draft(draft, _movement_contract())


def test_unknown_action_resolution_still_fails_closed() -> None:
    draft = TurnOutcomeDecisionDraft.model_validate(
        {"action_outcomes": [{"action_index": 0, "resolution": "maybe"}]}
    )

    with pytest.raises(TurnPlanningError, match="unknown resolution"):
        normalize_outcome_draft(draft, _movement_contract())

def test_typed_outcome_keeps_its_reaction_and_drops_an_unbound_beat() -> None:
    draft = TurnOutcomeDecisionDraft.model_validate(
        {
            "action_outcomes": [
                {
                    "action_index": 0,
                    "resolution": "auto_success",
                    "safe_mundane": True,
                    "observable_outcome": "Мария и Анна открыли ставни.",
                    "reaction": "Мария на миг поднимает взгляд.",
                }
            ],
            "character_beats": ["Мария и Анна продолжают свои обычные дела."],
        }
    )

    decision = normalize_outcome_draft(draft, _movement_contract())

    assert decision.character_beats == ["Мария на миг поднимает взгляд."]
    assert "Мария и Анна открыли ставни." in decision.observable_consequences


def test_invalid_npc_introductions_are_dropped_without_failing_outcome_draft() -> None:
    """CJK/garbage grounded roles must not kill ExactTurnOutcomeDecisionDraft."""
    from app.services.turn_outcome_resolver import _outcome_wire_model

    wire = _outcome_wire_model(1, allow_choice=True)
    draft = wire.model_validate(
        {
            "action_outcomes": [
                {
                    "action_index": 0,
                    "resolution": "auto_success",
                    "safe_mundane": True,
                    "observable_outcome": "Кай спокойно выходит в коридор.",
                }
            ],
            "npc_introductions": [
                {
                    "canonical_name": "未知的人",
                    "role": "未知角色",
                    "description": "A completely fabricated person description that is long enough.",
                    "appearance": "A completely fabricated appearance string that is long enough.",
                    "temporary_name": True,
                    "reason": "Model invented a person without grounded role evidence.",
                },
                {
                    "canonical_name": "Дежурный",
                    "role": "дежурный",
                    "description": "Дежурный в тёмной рабочей куртке спокойно стоит у входа в помещение.",
                    "appearance": "Короткие волосы, простая одежда и связка служебных ключей на поясе.",
                    "temporary_name": True,
                    "reason": "Игрок непосредственно встречает дежурного у входа.",
                },
            ],
        }
    )

    assert len(draft.npc_introductions) == 1
    assert draft.npc_introductions[0].canonical_name == "Дежурный"
    decision = normalize_outcome_draft(draft, _movement_contract())
    assert len(decision.npc_introductions) == 1
    assert decision.action_outcomes[0].resolution == "auto_success"


def test_blocked_outcome_requires_authoritative_evidence_quote() -> None:
    wire = _outcome_wire_model(
        1,
        allow_choice=False,
        evidence="Мария ранее сказала: «Я не принимаю таких условий».",
    )

    with pytest.raises(ValueError, match="authoritative-context evidence"):
        wire.model_validate(
            {
                "npc_introductions": [],
                "action_outcomes": [
                    {
                        "action_index": 0,
                        "resolution": "blocked",
                        "blocking_reason": "Мария отказывается.",
                        "blocking_evidence_quote": "Мне просто не нравится запрос игрока.",
                    }
                ]
            }
        )

    parsed = wire.model_validate(
        {
            "npc_introductions": [],
            "action_outcomes": [
                {
                    "action_index": 0,
                    "resolution": "blocked",
                    "blocking_reason": "Мария отказывается от этих условий.",
                    "blocking_evidence_quote": "Я не принимаю таких условий",
                }
            ]
        }
    )
    assert parsed.action_outcomes[0].resolution == "blocked"


def test_blocker_selects_source_id_without_copying_quote() -> None:
    wire = _outcome_wire_model(
        1, allow_choice=False,
        evidence="[Current Scene]\nМария ранее сказала: «Я не принимаю таких условий».\nДверь открыта.",
    )
    parsed = wire.model_validate({
        "npc_introductions": [],
        "action_outcomes": [{
            "action_index": 0, "resolution": "blocked",
            "blocking_reason": "Мария отказывается от этих условий.",
            "blocking_evidence_ref": "E1",
        }],
    })
    assert parsed.action_outcomes[0].resolution == "blocked"
    assert parsed.action_outcomes[0].blocking_evidence_quote == (
        "Мария ранее сказала: «Я не принимаю таких условий»."
    )
    with pytest.raises(ValueError):
        wire.model_validate({
            "npc_introductions": [],
            "action_outcomes": [{
                "action_index": 0, "resolution": "blocked",
                "blocking_reason": "Мария отказывается.", "blocking_evidence_ref": "E99",
            }],
        })


def test_existing_addressee_cannot_be_reintroduced_as_new_person() -> None:
    wire = _outcome_wire_model(0, allow_choice=False, allow_introductions=False)
    assert wire.model_validate({
        "action_outcomes": [], "npc_introductions": [],
        "observable_consequences": ["Мария отвечает на вопрос."],
    }).action_outcomes == []
    with pytest.raises(ValueError):
        wire.model_validate({
            "action_outcomes": [],
            "npc_introductions": [{
                "canonical_name": "Мария", "role": "дежурная",
                "description": "Дежурная в тёмной рабочей куртке спокойно стоит у входа.",
                "appearance": "Короткие волосы, простая одежда и связка ключей на поясе.",
                "reason": "Отвечает на вопрос.",
            }],
            "observable_consequences": ["Мария отвечает на вопрос."],
        })


def test_evidence_matching_tolerates_context_line_wrapping() -> None:
    wire = _outcome_wire_model(1, allow_choice=False, evidence="Дверь закрыта\nна засов.")
    parsed = wire.model_validate({
        "npc_introductions": [],
        "action_outcomes": [{
            "action_index": 0, "resolution": "blocked", "blocking_reason": "Засов закрыт.",
            "blocking_evidence_quote": "закрыта на засов",
        }],
    })
    assert parsed.action_outcomes[0].resolution == "blocked"


def test_long_source_anchor_survives_provider_resolver_roundtrip() -> None:
    wire = _outcome_wire_model(1, allow_choice=False, evidence="Дверь заперта. " * 55)
    payload = {
        "npc_introductions": [],
        "action_outcomes": [{"action_index": 0, "resolution": "blocked",
                             "blocking_reason": "Дверь заперта.", "blocking_evidence_ref": "E0"}],
    }
    first = wire.model_validate(payload).model_dump(mode="json")
    assert wire.model_validate(first).action_outcomes[0].resolution == "blocked"


def test_addressed_question_requires_actual_reply_not_waiting_gesture():
    wire = _outcome_wire_model(
        0, allow_choice=False, allow_introductions=False, requires_response=True,
    )
    with pytest.raises(ValueError, match="direct_response"):
        wire.model_validate({
            "action_outcomes": [], "npc_introductions": [],
            "observable_consequences": ["Контактное лицо кивает и готовится ответить."],
        })
    contract = PlayerIntentContract(summary="Спрашиваю, откуда он знает моё имя.",
                                    addressed_response_requested=True)
    draft = wire.model_validate({
        "action_outcomes": [], "npc_introductions": [],
        "direct_response": "Контактное лицо отвечает: «Я не знаю вашего имени»." ,
        "observable_consequences": ["Собеседник опускает руку."],
    })
    result = normalize_outcome_draft(draft, contract)
    assert result.observable_consequences[0] == draft.direct_response
    assert result.npc_introductions == []


def test_unsupported_ordinary_travel_block_resolves_without_invented_obstacle() -> None:
    wire = _outcome_wire_model(
        1,
        allow_choice=False,
        evidence="Вход открыт. Обычный проход доступен.",
        ordinary_movement_destinations={0: "Прачечная соседнего дома"},
    )

    parsed = wire.model_validate({
        "npc_introductions": [],
        "action_outcomes": [{
            "action_index": 0,
            "resolution": "blocked",
            "blocking_reason": "Путь закрыт.",
            "blocking_evidence_quote": "Запертая дверь",
        }],
        "canon_constraints": ["Не пускать игрока в прачечную."],
    })

    assert parsed.action_outcomes[0].resolution == "auto_success"
    assert parsed.action_outcomes[0].blocking_reason is None
    assert parsed.canon_constraints == []
    assert parsed.observable_consequences == [
        "Переход в место «Прачечная соседнего дома» завершён."
    ]


def test_observation_can_report_newly_discovered_negative_result_without_context_quote() -> None:
    wire = _outcome_wire_model(
        1,
        allow_choice=False,
        evidence="В комнате лежат системные журналы.",
        observation_indices={0},
    )
    parsed = wire.model_validate({
        "npc_introductions": [],
        "action_outcomes": [{
            "action_index": 0,
            "resolution": "blocked",
            "blocking_reason": "В доступных записях нет следов взлома.",
            "observable_outcome": "Проверенные журналы не содержат следов взлома.",
        }],
    })
    assert parsed.action_outcomes[0].resolution == "blocked"
    assert parsed.action_outcomes[0].blocking_evidence_quote is None


def test_world_state_question_stamps_direct_answer_obligation() -> None:
    contract = PlayerIntentContract.model_validate(
        {
            "summary": "Во что сейчас одета Мария?",
            "actions": [],
            "world_state_question": True,
        }
    )
    decision = normalize_outcome_draft(
        TurnOutcomeDecisionDraft.model_validate(
            {
                "action_outcomes": [],
                "observable_consequences": ["Мария сейчас полностью обнажена."],
            }
        ),
        contract,
    )

    stamped = stamp_world_state_answer(decision, contract)

    assert any("WORLD STATE ANSWER" in item for item in stamped.canon_constraints)
    assert any("direct answer" in item for item in stamped.narration_guidance)
