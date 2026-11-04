"""QA-302 regressions: repair."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.models.item_state import ItemStateSnapshot
from app.models.turn_authority import TurnAuthority
from app.services.narration_publication_guard import NarrationPublicationGuard
from app.services.turn_authority_validator import TurnAuthorityValidator


def test_narrowed_contract_preserves_the_earned_interaction_with_items_in_scene():
    result = "Паромщик подтверждает заказ и назначает встречу на утро."
    authority = TurnAuthority(campaign_id=uuid4(), trigger_turn_id=uuid4(), player_input="Спрашиваю о работе.",
        item_states=[ItemStateSnapshot(item_id=uuid4(), name="Канат", location_id=uuid4())],
        action_sequence={"steps": [{"action_type": "interaction", "status": "completed",
                                    "observable_outcome": result}]})
    assert result in NarrationPublicationGuard.render_contract(authority)



def test_narrowed_contract_keeps_the_typed_world_development_and_response():
    authority = TurnAuthority(campaign_id=uuid4(), trigger_turn_id=uuid4(), player_input="Слушаю.",
        item_states=[ItemStateSnapshot(item_id=uuid4(), name="Канат", location_id=uuid4())],
        observable_consequences=["Служащий возвращается с результатом проверки журнала."])
    assert authority.observable_consequences[0] in NarrationPublicationGuard.render_contract(authority)












def test_a_repaired_publication_is_not_classified_good_without_result_coverage():
    from app.services.playtest_trace import _quality_classification

    audit = {"draft_text": "Большая сцена с разговором.", "final_text": "Ты остаёшься здесь.",
             "violation_count": 1}
    quality = _quality_classification(audit, "repaired", "Ты остаёшься здесь.", [])
    assert quality["published"] == "unknown"
    certified = _quality_classification(audit, "repaired", "Ты остаёшься здесь.", [],
                                        publication_guard={"outcomes_verified": True})
    assert certified["published"] == "good"

