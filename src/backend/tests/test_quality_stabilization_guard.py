from __future__ import annotations

from app.models.proposed_change import ChangeType, ProposedChangeCreate
from app.services.post_turn_structured_receipt_guard import _explicit_item_debt_fulfillments
from app.services.quality_stabilization_guard import (
    _PLANNER_CALL_BUDGET,
    _has_completed_interaction_receipt,
    _quoted_spans,
    _stabilize_narrator_proposals,
)


def _proposal(
    change_type: ChangeType,
    *,
    evidence: str,
    outcome_id: str = "o1",
) -> ProposedChangeCreate:
    payload = {
        "_canon": {
            "outcome_id": outcome_id,
            "evidence": evidence,
        }
    }
    if change_type == ChangeType.FACT:
        payload.update(
            {
                "subject": "склад",
                "predicate": "принадлежит",
                "object_value": "компании Север",
            }
        )
    elif change_type == ChangeType.KNOWLEDGE:
        payload.update(
            {
                "recipient_id": "00000000-0000-4000-8000-000000000001",
                "source_character_id": "00000000-0000-4000-8000-000000000002",
                "proposition": evidence,
            }
        )
    return ProposedChangeCreate(change_type=change_type, payload=payload)


def test_quoted_spans_extracts_supported_quote_styles_without_narrator_text() -> None:
    text = (
        "Мартин кивает. «Склад принадлежит компании Север». "
        "Он добавляет: “Архив закрыт”. Потом говорит: \"Ключ у меня\"."
    )

    assert _quoted_spans(text) == [
        "Склад принадлежит компании Север",
        "Архив закрыт",
        "Ключ у меня",
    ]


def test_objective_fact_backed_only_by_npc_quote_is_dropped() -> None:
    quote = "По моему мнению, склад принадлежит компании Север"
    fact = _proposal(
        ChangeType.FACT,
        evidence=quote + ". — Мартин Вэнс",
    )

    assert _stabilize_narrator_proposals([fact], [quote]) == []


def test_broad_actor_segment_is_dropped_even_when_it_selected_a_real_quote() -> None:
    player_quote = "Я полностью выполняю условие нашего долга"
    wrongly_attributed = _proposal(
        ChangeType.KNOWLEDGE,
        evidence=player_quote,
        outcome_id="actor-segment-5",
    )

    assert _stabilize_narrator_proposals([wrongly_attributed], [player_quote]) == []


def test_narrow_quoted_claim_provenance_survives_filter() -> None:
    npc_quote = "По моему мнению, склад принадлежит компании Север"
    knowledge = _proposal(
        ChangeType.KNOWLEDGE,
        evidence=npc_quote,
        outcome_id="quoted-claim-1",
    )

    assert _stabilize_narrator_proposals([knowledge], [npc_quote]) == [knowledge]


def test_nonquoted_actor_segment_knowledge_is_dropped() -> None:
    narrator_prose = "Голос выходит низким и ровным, лишенным лишних интонаций."
    knowledge = _proposal(
        ChangeType.KNOWLEDGE,
        evidence=narrator_prose,
        outcome_id="actor-segment-10",
    )

    assert _stabilize_narrator_proposals([knowledge], []) == []


def test_nonquoted_observable_fact_survives_provenance_filter() -> None:
    fact = _proposal(
        ChangeType.FACT,
        evidence="Свет в комнате включается, освещая пространство.",
    )

    assert _stabilize_narrator_proposals([fact], []) == [fact]


def test_interaction_receipt_detection_uses_completed_executor_payload() -> None:
    receipts = [
        {
            "description": "Свет в комнате включается, освещая пространство.",
            "payload": {
                "status": "completed",
                "action_type": "interaction",
            },
        }
    ]
    blocked = [
        {
            "description": "Попытка не завершилась.",
            "payload": {
                "status": "blocked",
                "action_type": "interaction",
            },
        }
    ]

    assert _has_completed_interaction_receipt(receipts) is True
    assert _has_completed_interaction_receipt(blocked) is False


def test_exact_item_give_matches_only_item_backed_debt() -> None:
    from types import SimpleNamespace
    from uuid import UUID

    player = UUID("00000000-0000-4000-8000-000000000001")
    martin = UUID("00000000-0000-4000-8000-000000000002")
    debt_id = UUID("00000000-0000-4000-8000-000000000003")
    unrelated_id = UUID("00000000-0000-4000-8000-000000000004")
    relationships = [
        SimpleNamespace(
            id=str(debt_id),
            subject_id=str(martin),
            object_id=str(player),
            relation_type="debt",
            description="Кай должен Мартину вернуть латунный ключ; после возврата долг закрыт.",
        ),
        SimpleNamespace(
            id=str(unrelated_id),
            subject_id=str(martin),
            object_id=str(player),
            relation_type="friendship",
            description="Мартин считает Кая надёжным знакомым.",
        ),
    ]
    receipt = {
        "operation": "give",
        "item_name": "латунный ключ (Кай)",
        "from_character_id": str(player),
        "to_character_id": str(martin),
    }

    assert _explicit_item_debt_fulfillments(receipt, relationships) == {debt_id}


def test_planner_budget_makes_30_call_cascade_impossible() -> None:
    assert _PLANNER_CALL_BUDGET == 12
    assert _PLANNER_CALL_BUDGET < 30
