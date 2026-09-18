from __future__ import annotations

import pytest

from app.models.player_intent import PlayerIntentContract
from app.services.turn_outcome_resolver import (
    TurnOutcomeDecisionDraft,
    normalize_outcome_draft,
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
