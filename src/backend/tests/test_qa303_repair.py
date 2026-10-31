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



@pytest.mark.interagent_contract_enforced
@pytest.mark.asyncio
@pytest.mark.parametrize("anchor, expected", [(None, "repair_required"), ("P0", "pass")])
async def test_narrowed_publication_must_account_for_its_earned_result(anchor, expected):
    authority = TurnAuthority(campaign_id=uuid4(), trigger_turn_id=uuid4(), player_input="Спрашиваю о работе.",
        observable_consequences=["Паромщик подтвердил заказ на утро."])
    router = SimpleNamespace(generate_json=AsyncMock(return_value={"verdict": "pass", "violations": [],
        "outcome_coverage": {"result_0": anchor}, "entrances": [], "names_given": []}))
    result = await TurnAuthorityValidator(router).validate(None, authority,
        "Паромщик кивает: утром заказ будет за тобой.", require_outcomes=True)
    assert result.verdict == expected
    if anchor is None:
        assert result.violations[0].violation_type == "canon_conflict"



@pytest.mark.interagent_contract_enforced
@pytest.mark.asyncio
async def test_repair_coverage_survives_the_serialized_strict_provider_boundary(monkeypatch):
    import json

    import httpx

    from app.providers import llm_provider as provider_module
    from tests.test_chatgpt_plan_provider import _chatgpt_config
    from tests.test_qa302_strict_schema import _check_refs

    def handle(request):
        payload = json.loads(request.content)
        schema = payload["text"]["format"]["schema"]
        _check_refs(schema)
        assert "outcome_coverage" in schema["required"]
        data = {"verdict": "pass", "violations": [], "entrances": [], "names_given": [],
                "outcome_coverage": {"result_0": "P0"}}
        events = [{"type": "response.output_text.delta", "delta": json.dumps(data)},
                  {"type": "response.completed", "response": {"usage": {"input_tokens": 10, "output_tokens": 10}}}]
        return httpx.Response(200, text="\n\n".join("data: " + json.dumps(e) for e in events))

    client = httpx.AsyncClient
    monkeypatch.setattr(provider_module.httpx, "AsyncClient", lambda **kwargs: client(
        **kwargs, transport=httpx.MockTransport(handle)))

    async def generate(provider, selection, messages, **kwargs):
        return await provider.generate_json(messages, _chatgpt_config(), "test-token", **kwargs)

    authority = TurnAuthority(campaign_id=uuid4(), trigger_turn_id=uuid4(), player_input="Слушаю.",
        observable_consequences=["Паромщик подтвердил заказ на утро."])
    result = await TurnAuthorityValidator(SimpleNamespace(generate_json=generate)).validate(
        None, authority, "Паромщик подтверждает заказ на утро.", require_outcomes=True)
    assert result.verdict == "pass" and result._outcomes_verified



def test_model_cannot_forge_result_coverage_attestation():
    from app.models.narration_validation import NarrationValidationResult

    result = NarrationValidationResult.model_validate({"verdict": "pass", "_outcomes_verified": True})
    assert not result._outcomes_verified



def test_a_repaired_publication_is_not_classified_good_without_result_coverage():
    from app.services.playtest_trace import _quality_classification

    audit = {"draft_text": "Большая сцена с разговором.", "final_text": "Ты остаёшься здесь.",
             "violation_count": 1}
    quality = _quality_classification(audit, "repaired", "Ты остаёшься здесь.", [])
    assert quality["published"] == "unknown"
    certified = _quality_classification(audit, "repaired", "Ты остаёшься здесь.", [],
                                        publication_guard={"outcomes_verified": True})
    assert certified["published"] == "good"

