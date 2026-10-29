"""QA-302: exercise the schema sent on the real structured-output transport boundary."""
import json
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from app.models.item_state import ItemStateSnapshot
from app.models.narration_validation import NarrationValidationResult
from app.models.turn import ChatMessage
from app.models.turn_authority import TurnAuthority
from app.providers import llm_provider as provider_module
from app.providers.llm_provider import LLMProvider
from app.services.turn_authority_validator import entrance_model, inventory_validation_model
from tests.test_chatgpt_plan_provider import _chatgpt_config


def _check_refs(node):
    if isinstance(node, dict):
        if "$ref" in node:
            assert set(node) == {"$ref"}, f"Unsupported reference siblings: {node}"
        for value in node.values():
            _check_refs(value)
    elif isinstance(node, list):
        for value in node:
            _check_refs(value)


def _wire(count):
    authority = TurnAuthority(campaign_id=uuid4(), trigger_turn_id=uuid4(), player_input="Осматриваюсь.",
        item_states=[ItemStateSnapshot(item_id=uuid4(), name=f"Предмет {i}") for i in range(count)])
    model = inventory_validation_model(authority, "На пристани спокойно.") if count else NarrationValidationResult
    return authority, entrance_model(authority, model)


@pytest.mark.parametrize("count", [0, 1, 2, 8])
def test_inventory_wire_is_compatible_with_strict_reference_subset(count):
    authority, wire = _wire(count)
    original = wire.model_json_schema()
    _check_refs(original)
    schema = LLMProvider._strict_schema(LLMProvider._compact_schema(original))
    _check_refs(schema)
    if count:
        review_ref = schema["properties"]["item_reviews"]["$ref"].split("/")[-1]
        reviews = schema["$defs"][review_ref]
        assert set(reviews["required"]) == {str(item.item_id) for item in authority.item_states}
        for item in authority.item_states:
            target = reviews["properties"][str(item.item_id)]["$ref"].split("/")[-1]
            review = schema["$defs"][target]
            assert review["properties"]["item_id"]["const"] == str(item.item_id)
            assert review["description"]
        data = {"item_reviews": {str(item.item_id): {"item_id": str(item.item_id),
            "reference": "absent", "claims": []} for item in authority.item_states},
            "verdict": "pass", "violations": [], "entrances": [], "names_given": []}
        assert wire.model_validate(data).verdict == "pass"
        first = str(authority.item_states[0].item_id)
        data["item_reviews"][first]["item_id"] = str(uuid4())
        with pytest.raises(ValidationError):
            wire.model_validate(data)


@pytest.mark.asyncio
async def test_registered_item_validation_survives_serialized_responses_request(monkeypatch):
    authority, wire = _wire(2)
    item_reviews = {str(item.item_id): {"item_id": str(item.item_id), "reference": "absent", "claims": []}
                    for item in authority.item_states}
    expected = {"item_reviews": item_reviews, "verdict": "pass", "summary": "", "violations": [],
                "entrances": [], "names_given": []}

    def handle(request):
        payload = json.loads(request.content)
        # Mimic the strict endpoint's schema rejection, not an unconditional successful model mock.
        try:
            assert payload["text"]["format"]["strict"] is True
            _check_refs(payload["text"]["format"]["schema"])
        except (AssertionError, KeyError) as exc:
            return httpx.Response(400, json={"error": {"message": str(exc)}})
        events = [{"type": "response.output_text.delta", "delta": json.dumps(expected)},
                  {"type": "response.completed", "response": {"usage": {"input_tokens": 10, "output_tokens": 10}}}]
        return httpx.Response(200, text="\n\n".join("data: " + json.dumps(event) for event in events))

    client_type = httpx.AsyncClient
    monkeypatch.setattr(provider_module.httpx, "AsyncClient", lambda **kwargs: client_type(
        **kwargs, transport=httpx.MockTransport(handle)))
    provider = LLMProvider()
    result = await provider.generate_json([ChatMessage(role="user", content="Осматриваюсь.")],
        _chatgpt_config(), "test-token", response_model=wire)
    assert result["verdict"] == "pass"
    assert result["item_reviews"] == item_reviews
    assert provider.last_telemetry["attempt"] == 1
