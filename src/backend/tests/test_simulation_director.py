from pathlib import Path
import sys
from uuid import uuid4

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from run_realistic_simulation_v2 import (
    PlayerDecision,
    PlayerPolicy,
    SimulationState,
    TraceStore,
    deterministic_fallback_card,
    parse_player_decision,
)
from simulation_scenario import NPCS, PHASES


def test_scenario_introduces_all_npcs_gradually():
    introduced: set[str] = set()
    introduction_sizes = []
    for phase in PHASES:
        introduced.update(phase.introduced_npcs)
        introduction_sizes.append(len(introduced))
        assert set(phase.active_npcs).issubset(introduced)
        assert 2 <= len(phase.active_npcs) <= 5
        assert len(phase.pulses) >= 3
        assert len(phase.opening_theses) >= 3

    assert introduced == set(NPCS)
    assert introduction_sizes == sorted(introduction_sizes)
    assert introduction_sizes[0] < introduction_sizes[-1]


def test_player_parser_canonicalizes_active_npc_name():
    decision = parse_player_decision(
        '{"target":"sylvia","mode":"question","intent":"Я спрашиваю о руне."}',
        ["Sylvia", "Garrick"],
    )
    assert decision.target == "Sylvia"
    assert decision.mode == "question"


def test_player_policy_detects_russian_repetition():
    policy = PlayerPolicy()
    first = PlayerDecision(
        target="narrator",
        mode="action",
        intent="Я осматриваю крепления ворот и ищу следы недавнего износа.",
    )
    valid, _ = policy.validate(first, ["Sylvia"])
    assert valid is True
    policy.remember(first)

    valid, error = policy.validate(first, ["Sylvia"])
    assert valid is False
    assert "repeats" in error
    assert policy.repeated_actions == 1


def test_player_policy_rejects_russian_declared_outcome():
    policy = PlayerPolicy()
    outcome = PlayerDecision(
        target="narrator",
        mode="action",
        intent="Я успешно открываю ворота и обнаруживаю тайный проход.",
    )
    valid, error = policy.validate(outcome, ["Sylvia"])
    assert valid is False
    assert "outcome" in error
    assert policy.rejected_outcomes == 1


def test_contextual_fallbacks_do_not_repeat():
    policy = PlayerPolicy()
    fingerprints = []
    for turn in range(1, 7):
        decision = policy.fallback(
            ["Sylvia", "Garrick"],
            policy.preferred_mode(turn),
            "Выбрать путь в цитадель.",
            "Гаррик заметил свежие следы у западного оврага.",
            ["Карта неполна.", "До рассвета остаётся мало времени."],
            turn,
        )
        valid, error = policy.validate(decision, ["Sylvia", "Garrick"])
        assert valid is True, error
        policy.remember(decision)
        fingerprints.append(policy.fingerprint(decision.intent))

    assert len(set(fingerprints)) == len(fingerprints)
    assert policy.fallbacks == 6


def test_player_policy_rejects_inactive_target():
    policy = PlayerPolicy()
    decision = PlayerDecision(
        target="Thorin",
        mode="dialogue",
        intent="Я спрашиваю Торина о печати.",
    )
    valid, error = policy.validate(decision, ["Sylvia", "Garrick"])
    assert valid is False
    assert "not active" in error


def test_trace_store_upserts_logical_turn(tmp_path):
    path = tmp_path / "trace.jsonl"
    store = TraceStore(path)
    store.upsert({"turn": 4, "dm": "старый ответ"})
    store.upsert({"turn": 4, "dm": "исправленный ответ"})
    store.upsert({"turn": 5, "dm": "следующий ответ"})

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    restored = TraceStore(path)
    assert restored.records[4]["dm"] == "исправленный ответ"
    assert sorted(restored.records) == [4, 5]


def test_simulation_state_round_trip(tmp_path):
    path = tmp_path / "state.json"
    state = SimulationState(
        run_id="run-1",
        campaign_id=str(uuid4()),
        logical_turn=17,
        phase_index=2,
        phase_turn=5,
        injected_pulses=[0, 1],
        confirmed_pulses=[0],
    )
    state.save(path)

    restored = SimulationState.load(path)
    assert restored is not None
    assert restored.run_id == "run-1"
    assert restored.logical_turn == 17
    assert restored.injected_pulses == [0, 1]
    assert restored.confirmed_pulses == [0]


def test_fallback_cards_are_distinct_and_owner_specific():
    location_id = uuid4()
    sylvia = deterministic_fallback_card(NPCS["Sylvia"], location_id)
    garrick = deterministic_fallback_card(NPCS["Garrick"], location_id)

    assert sylvia.voice != garrick.voice
    assert sylvia.personality != garrick.personality
    assert sylvia.equipment != garrick.equipment
    assert all("Sylvia" in item for item in sylvia.equipment)
    assert all("Garrick" in item for item in garrick.equipment)
    assert sylvia.visual_profile["fallback"] is True
