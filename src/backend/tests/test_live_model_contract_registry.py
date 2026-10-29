from types import SimpleNamespace
from unittest.mock import Mock

from live_model_contracts.registry import all_cases


def test_default_cli_suite_includes_all_registered_contracts(monkeypatch):
    from live_model_contracts.runner import _parse_args, _select_cases
    monkeypatch.setattr('sys.argv', ['test-models'])
    assert _parse_args().suite == 'all'
    assert {case.id for case in _select_cases(_parse_args())} == {case.id for case in all_cases()}
    monkeypatch.setattr('sys.argv', ['test-models', '--suite', 'core'])
    assert _parse_args().suite == 'core'


def test_live_model_contract_ids_and_transition_ownership_are_explicit():
    cases = list(all_cases())
    ids = [case.id for case in cases]

    assert len(cases) >= 24
    assert len(ids) == len(set(ids))
    assert all(case.transitions for case in cases)
    assert all(case.turns and all(turn.strip() for turn in case.turns) for case in cases)

    required_transition_families = {
        "scene",
        "movement",
        "location",
        "presence",
        "character",
        "identity",
        "item",
        "time",
        "knowledge",
        "fact",
        "relationship",
        "thesis",
        "event",
        "canon",
        "compound",
        "undo",
        "turn",
    }
    owned = {
        transition.split(".", 1)[0]
        for case in cases
        for transition in case.transitions
    }
    assert required_transition_families <= owned


def test_core_live_contracts_are_hard_invariants():
    core = [case for case in all_cases() if case.suite == "core"]

    assert core
    assert all(case.min_pass_rate == 1.0 for case in core)


def test_contact_contract_enters_scene_before_creating_unknown_npc():
    case = next(case for case in all_cases() if case.id == "new_npc_direct_contact")

    assert len(case.turns) == 2
    assert "контор" in case.turns[0].casefold()
    assert "дежурн" in case.turns[1].casefold()


def test_name_reveal_fixture_establishes_scene_presence_not_only_character_location():
    from live_model_contracts.world import FixtureWorld

    case = next(case for case in all_cases() if case.id == "npc_temporary_to_stable_identity")
    world = FixtureWorld("campaign", "hero", "starting-scene", locations={"Контора": "office"})
    client = SimpleNamespace(post=Mock(side_effect=[
        SimpleNamespace(status_code=201, json=lambda: {"id": "attendant"}),
        SimpleNamespace(status_code=201, json=lambda: {"id": "office-scene"}),
        SimpleNamespace(status_code=200),
    ]))

    case.prepare(client, world)

    assert world.extra["temporary_npc_id"] == "attendant"
    assert client.post.call_args_list[1].kwargs["params"] == {"activate": False}
    assert client.post.call_args_list[1].kwargs["json"]["location_id"] == "office"
    assert client.post.call_args_list[2].args == ("/api/scenes/office-scene/participants",)
    assert client.post.call_args_list[2].kwargs["params"] == {"entity_id": "attendant"}
