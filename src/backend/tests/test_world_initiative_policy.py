"""The world acts on its own under a typed, per-master initiative policy."""
import pytest
from pydantic import ValidationError

from app.models.game_master import InitiativePolicy
from app.services.master_catalog import get_preset, list_presets
from app.services.turn_outcome_resolver import _outcome_wire_model

THREADS = {"thread_0": False, "thread_1": True}
BASE = {"action_outcomes": [], "npc_introductions": [], "observable_consequences": ["Тихо."]}


def _wire(**kwargs):
    return _outcome_wire_model(0, world_threads=THREADS, **kwargs)


def test_due_initiative_makes_a_thread_world_move_mandatory():
    wire = _wire(world_kinds=("information",), world_required=True)
    assert "world_move" in wire.model_json_schema()["required"]
    with pytest.raises(ValidationError):
        wire.model_validate(BASE)
    move = {"thread_id": "thread_0", "kind": "information", "outcome": "Приходит весть с пристани."}
    assert wire.model_validate({**BASE, "world_move": move}).world_move.thread_id == "thread_0"


def test_world_move_names_only_known_threads_and_master_kinds():
    wire = _wire(world_kinds=("information",))
    assert wire.model_validate(BASE).world_move is None
    for move in ({"thread_id": "thread_9", "kind": "information", "outcome": "Приходит весть с пристани."},
                 {"thread_id": "thread_0", "kind": "pressure", "outcome": "Приходит весть с пристани."}):
        with pytest.raises(ValidationError):
            wire.model_validate({**BASE, "world_move": move})


def test_npc_kinds_need_an_actor_thread_and_arrival_needs_a_typed_person():
    wire = _wire(world_kinds=("npc_action", "arrival"))
    with pytest.raises(ValidationError):
        wire.model_validate({**BASE, "world_move": {
            "thread_id": "thread_0", "kind": "npc_action", "outcome": "Хозяин сам идёт к двери."}})
    with pytest.raises(ValidationError):
        wire.model_validate({**BASE, "world_move": {
            "thread_id": "thread_0", "kind": "arrival", "outcome": "В трактир входит речник."}})


def test_no_threads_means_no_world_move_channel():
    wire = _outcome_wire_model(0, world_kinds=("information",), world_required=True)
    assert wire.model_validate(BASE).world_move is None


def test_masters_differ_in_patience_kinds_and_harshness():
    policies = {preset.id: preset.initiative for preset in list_presets()}
    assert len({(p.patience_turns, p.harshness, tuple(p.world_moves)) for p in policies.values()}) == len(policies)
    keeper, referee = get_preset("soft_keeper").initiative, get_preset("harsh_referee").initiative
    assert keeper.patience_turns > referee.patience_turns
    assert (keeper.harshness, referee.harshness) == ("gentle", "harsh")
    assert "pressure" not in keeper.world_moves and "pressure" in referee.world_moves
    assert InitiativePolicy().patience_turns == 3


def test_arrival_of_a_threads_own_person_needs_no_new_introduction():
    # The river-man sent for at errand time is the thread's actor; the world brings that ID.
    wire = _wire(world_kinds=("arrival",))
    move = {"thread_id": "thread_1", "kind": "arrival", "outcome": "В трактир входит речник."}
    assert wire.model_validate({**BASE, "world_move": move}).world_move.kind == "arrival"


def test_sent_for_person_is_typed_offscreen_not_present():
    from app.models.player_intent import PlayerIntentContract
    from app.services.turn_outcome_resolver import normalize_outcome_draft
    person = {"canonical_name": "Речник", "role": "речник", "reason": "Хозяин послал за ним.",
              "description": "Речник, недавно вернувшийся с Ладоги на своей лодке.",
              "appearance": "Обветренный мужчина в смолёной куртке и высоких сапогах."}
    wire = _outcome_wire_model(1)
    draft = wire.model_validate({"action_outcomes": [{"action_index": 0, "resolution": "auto_success",
        "observable_outcome": "Хозяин посылает за речником.", "sends_for": person}], "npc_introductions": []})
    contract = PlayerIntentContract(summary="Пошлите за речником.", actions=[
        {"action_type": "interaction", "intent": "Попросить послать за речником."}])
    [npc] = normalize_outcome_draft(draft, contract).npc_introductions
    assert npc.sent_for and not npc.arrives


def test_world_does_not_repeat_its_last_kind_while_another_is_allowed():
    schema = _wire(world_kinds=("pressure", "information"), last_world_kind="pressure").model_json_schema()
    assert "pressure" not in str(schema["$defs"]["ThreadWorldMove"]["properties"]["kind"])
    only = _wire(world_kinds=("pressure",), last_world_kind="pressure").model_json_schema()
    assert "pressure" in str(only["$defs"]["ThreadWorldMove"]["properties"]["kind"])


def test_outcome_call_sees_whose_world_it_is():
    from app.services.master_director import outcome_master_block
    blocks = {p.id: outcome_master_block(p, ["npc_initiative"]) for p in list_presets()}
    assert all(p.brief.strip() in blocks[p.id] and "npc_initiative" in blocks[p.id] for p in list_presets())
    assert len(set(blocks.values())) == len(blocks)
