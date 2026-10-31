"""Live runs v274..v278 lost 12-20 s per hidden provider retry on three self-contradictions.
Each is now decided by the schema or the draft's own typed choice, never by a repair round-trip."""
from app.models.turn import ChatMessage
from app.services.player_intent_interpreter import _intent_wire_model
from app.services.turn_outcome_resolver import _outcome_wire_model

PLAYER = "00000000-0000-4000-8000-000000000002"
NPC = "00000000-0000-4000-8000-000000000003"


def test_self_address_is_no_addressee_and_present_people_are_offered_as_a_choice():
    wire = _intent_wire_model([ChatMessage(role="system", content=(
        f"Controlled character: {PLAYER}\nPhysically present characters: Тарин [id={PLAYER}], Борм [id={NPC}]"))])
    draft = wire.model_validate({"summary": "Спрашиваю.", "actions": [], "addressed_character_name": "Тарин"})
    assert draft.addressed_character_name is None
    schema = wire.model_json_schema()["properties"]["addressed_character_name"]
    assert {"const": "Борм", "type": "string"} in schema["anyOf"] or any(
        option.get("enum") == ["Борм"] or option.get("const") == "Борм" for option in schema["anyOf"])


def test_a_move_on_a_thread_its_own_review_closed_is_void_not_a_retry():
    source = "Выдать бланк после представления инспектора."
    wire = _outcome_wire_model(0, world_threads={"thread_0": True}, world_kinds=("npc_action",),
                               thread_sources={"thread_0": {"source": source}},
                               review_evidence=f"{source}\nИнспектор назвал имя.")
    draft = wire.model_validate({"action_outcomes": [], "npc_introductions": [], "observable_consequences": ["Тихо."],
        "thread_reviews": {"thread_0": {"status": "resolved", "evidence": "E1"}},
        "world_move": {"thread_id": "thread_0", "kind": "npc_action", "outcome": "Оператор снова требует имя."}})
    assert draft.world_move is None and draft.thread_reviews.thread_0.status == "resolved"


def test_npc_role_shape_is_a_decoder_constraint():
    role = _outcome_wire_model(0).model_json_schema()["$defs"]["IndexedNpcIntroductionDraft"]["properties"]["role"]
    assert role["maxLength"] == 40 and role["pattern"]


def _base(**extra):
    return {"action_outcomes": [{"action_index": 0, "resolution": "auto_success", "safe_mundane": True,
                                 "observable_outcome": "Серебряк на ладони."}],
            "npc_introductions": [], "observable_consequences": ["Тихо."], **extra}


def test_a_count_written_as_a_property_is_dropped_when_the_split_is_typed():
    """verify-r3 T5/T6b: quantity in item_property_changes beside a typed split cost a retry."""
    from uuid import uuid4
    from app.models.item_state import ItemStateSnapshot
    purse = ItemStateSnapshot(item_id=uuid4(), name="Кошель", owner_id=uuid4(), quantity=5)
    wire = _outcome_wire_model(1, allow_choice=False, required_effects={0: "item_change"}, handoff_states=[purse])
    draft = wire.model_validate(_base(
        item_property_changes=[{"item_id": str(purse.item_id), "action_index": 0,
                                "property_name": "quantity", "value": "4"}],
        item_introductions=[{"canonical_name": "серебряк", "evidence": "Серебряк на ладони.",
                             "owned_by_player": True, "after_action_index": 0,
                             "taken_from_item_id": str(purse.item_id), "quantity": 1}]))
    assert draft.item_property_changes == [] and draft.item_introductions[0].quantity == 1


def test_response_speaker_is_a_closed_choice_of_present_people():
    """verify-r3 T8: a free-text speaker outside the cast cost a retry."""
    wire = _outcome_wire_model(1, present_names=["Тарин", "Борм", "Ивен"])
    options = wire.model_json_schema()["properties"]["response_speaker_name"]["anyOf"]
    assert any(set(o.get("enum", [])) >= {"Борм", "Ивен"} for o in options)
