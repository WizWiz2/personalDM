from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from tests.simulation_reliable_fixture import (
    FIXTURE_PATH,
    collect_provenance,
    deterministic_fixture_character_card,
    fixed_ensure_phase_available,
    fixture_sha256,
    load_reliable_fixture,
)
from tests.simulation_reliable_soak import SCRIPTED_TURNS


def test_reliable_fixture_has_enough_valid_phases_and_stable_roster():
    catalog = load_reliable_fixture()
    phases = catalog.runtime_phases()
    npcs = catalog.runtime_npcs()

    assert len(phases) == 8
    assert set(npcs) == {"Мария", "Игорь Купец", "Владимир", "Лена"}
    assert len({phase.slug for phase in phases}) == len(phases)
    assert len({phase.title.casefold() for phase in phases}) == len(phases)

    introduced: set[str] = set()
    for phase in phases:
        introduced.update(name.casefold() for name in phase.introduced_npcs)
        assert all(name.casefold() in introduced for name in phase.active_npcs)
        active = {name.casefold() for name in phase.active_npcs}
        assert all(
            name.casefold() in active
            for thesis in phase.opening_theses
            for name in thesis.related_names
        )
        assert all(
            name.casefold() in active
            for pulse in phase.pulses
            for name in pulse.thesis.related_names
        )


@pytest.mark.asyncio
async def test_fixed_scenario_source_writes_exact_fixture_without_llm(tmp_path: Path):
    artifact = tmp_path / "scenario.json"
    catalog = await fixed_ensure_phase_available(
        path=artifact,
        reset=True,
        phase_index=0,
        provider=object(),
        router=object(),
        selection=object(),
        previous_outcomes=["ignored"],
    )

    assert len(catalog.runtime_phases()) == 8
    assert artifact.read_bytes() == FIXTURE_PATH.read_bytes()
    assert fixture_sha256(artifact) == fixture_sha256()


@pytest.mark.asyncio
async def test_fixed_scenario_source_fails_closed_when_fixture_is_exhausted(tmp_path: Path):
    with pytest.raises(RuntimeError, match="fixture exhausted"):
        await fixed_ensure_phase_available(
            path=tmp_path / "scenario.json",
            reset=True,
            phase_index=8,
            provider=None,
            router=None,
            selection=None,
            previous_outcomes=[],
        )


@pytest.mark.asyncio
async def test_fixture_character_builder_is_deterministic_and_complete():
    seed = load_reliable_fixture().runtime_npcs()["Мария"]
    location_id = uuid4()

    first, first_source = await deterministic_fixture_character_card(
        None, None, None, seed, location_id
    )
    second, second_source = await deterministic_fixture_character_card(
        None, None, None, seed, location_id
    )

    assert first_source == second_source == "fixture"
    assert first.model_dump() == second.model_dump()
    assert first.current_location_id == location_id
    assert first.appearance
    assert first.personality
    assert first.voice
    assert first.biography
    assert first.capabilities
    assert first.limitations


def test_reliable_provenance_contains_fixture_and_corpus_hashes():
    provenance = collect_provenance(SCRIPTED_TURNS, allow_dirty=True)

    assert len(provenance["commit"]) == 40
    assert len(provenance["fixture_sha256"]) == 64
    assert len(provenance["corpus_sha256"]) == 64
    assert provenance["fixture_version"] == "reliable-soak-world-v1"


def test_reliable_provenance_rejects_dirty_tree_by_default(monkeypatch, tmp_path: Path):
    from tests import simulation_reliable_fixture as fixture

    def fake_git_output(root: Path, *args: str) -> str:
        del root
        if args == ("rev-parse", "HEAD"):
            return "a" * 40
        if args[:2] == ("status", "--porcelain"):
            return " M src/backend/tests/simulation_quality_transport_fix.py"
        raise AssertionError(args)

    monkeypatch.setattr(fixture, "_git_output", fake_git_output)

    with pytest.raises(RuntimeError, match="clean git working tree"):
        fixture.collect_provenance(
            SCRIPTED_TURNS,
            repo_root=tmp_path,
            allow_dirty=False,
        )
