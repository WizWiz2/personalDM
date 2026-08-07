from types import SimpleNamespace

from tests.simulation_quality_transport_fix import (
    _normalize_player_target,
    _repair_arc_introductions,
    _simulation_mode,
)


def test_endurance_mode_is_explicitly_supported(monkeypatch):
    monkeypatch.setenv("PDM_SIM_MODE", "endurance")
    assert _simulation_mode() == "endurance"


def test_unknown_simulation_mode_falls_back_to_quality(monkeypatch):
    monkeypatch.setenv("PDM_SIM_MODE", "whatever")
    assert _simulation_mode() == "quality"


def test_player_target_strips_schema_placeholder_suffix():
    assert _normalize_player_target("Адриан|ActiveNpc", ["Адриан", "Лора"]) == "Адриан"


def test_single_active_npc_resolves_literal_placeholder():
    assert _normalize_player_target("ActiveNpc", ["Адриан"]) == "Адриан"


def test_ambiguous_placeholder_is_not_guessed():
    assert _normalize_player_target("ActiveNpc", ["Адриан", "Лора"]) == "ActiveNpc"


def test_unambiguous_transliteration_is_canonicalized():
    assert _normalize_player_target("Adrian", ["Адриан", "Лора"]) == "Адриан"


def test_arc_repair_only_introduces_known_new_active_npc():
    catalog = SimpleNamespace(canonical_npc_names=lambda: {"старик": "Старик"})
    npc = SimpleNamespace(name="Лорна")
    phase = SimpleNamespace(
        slug="first_encounter",
        introduced_npcs=[],
        active_npcs=["Старик", "Лорна"],
    )
    arc = SimpleNamespace(npcs=[npc], phases=[phase])

    repaired, repairs = _repair_arc_introductions(catalog, arc)

    assert repairs == [("first_encounter", "Лорна")]
    assert repaired.phases[0].introduced_npcs == ["Лорна"]
    assert arc.phases[0].introduced_npcs == []


def test_arc_repair_does_not_invent_unknown_npc():
    catalog = SimpleNamespace(canonical_npc_names=lambda: {})
    phase = SimpleNamespace(
        slug="first_encounter",
        introduced_npcs=[],
        active_npcs=["Неизвестный"],
    )
    arc = SimpleNamespace(npcs=[], phases=[phase])

    repaired, repairs = _repair_arc_introductions(catalog, arc)

    assert repairs == []
    assert repaired.phases[0].introduced_npcs == []
