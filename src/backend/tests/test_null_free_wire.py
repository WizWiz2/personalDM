"""Strict decoding makes every property required. A property that admits only one value (null,
an empty list) is pure output cost: the wire must not ask the model to write it."""
from uuid import uuid4

from app.providers.llm_provider import LLMProvider
from app.services.player_intent_interpreter import _IntentWire
from app.services.turn_outcome_resolver import _outcome_wire_model


def _wire(model):
    return LLMProvider._strict_schema(LLMProvider._compact_schema(model.model_json_schema()))


def _objects(schema):
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            yield schema
        for value in schema.values():
            yield from _objects(value)
    elif isinstance(schema, list):
        for value in schema:
            yield from _objects(value)


def _single_valued(schema):
    return [name for obj in _objects(schema) for name, prop in obj.get("properties", {}).items()
            if prop == {"type": "null"} or prop.get("maxItems") == 0]


def test_outcome_and_intent_wires_never_ask_for_a_forced_value():
    for model in (_outcome_wire_model(2), _outcome_wire_model(2, allow_choice=False), _IntentWire):
        assert _single_valued(_wire(model)) == []


def test_success_and_block_carry_only_their_own_fields():
    variants = [obj for obj in _objects(_wire(_outcome_wire_model(2, allow_choice=False)))
                if "resolution" in obj.get("properties", {}) and "action_index" in obj["properties"]]
    by_resolution = {tuple(v["properties"]["resolution"].get("enum", [v["properties"]["resolution"].get("const")])): v
                     for v in variants}
    assert {r for key in by_resolution for r in key} == {"auto_success", "blocked"}
    success = by_resolution[("auto_success",)]["properties"]
    blocked = by_resolution[("blocked",)]["properties"]
    assert not {"blocking_reason", "blocking_evidence_quote", "blocking_evidence_ref"} & success.keys()
    assert not {"sends_for", "carry_participants"} & blocked.keys()


def test_omitted_forced_fields_validate_to_their_only_value():
    wire = _outcome_wire_model(1, allow_choice=False)
    draft = wire.model_validate({
        "action_outcomes": [{"action_index": 0, "resolution": "auto_success", "safe_mundane": True,
                             "npc_response": "not_applicable", "arriving": None, "sends_for": None,
                             "observable_outcome": "Дверь открыта.", "reaction": None, "carry_participants": []}],
        "npc_introductions": [], "absent_people": [], "place_introductions": [], "item_introductions": [],
        "item_revelations": [], "item_property_changes": [],
    })
    outcome = draft.action_outcomes[0]
    assert (outcome.blocking_reason, outcome.blocking_evidence_ref, draft.npc_item_handoffs) == (None, None, [])
