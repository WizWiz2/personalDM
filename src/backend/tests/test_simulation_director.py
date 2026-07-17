from run_realistic_simulation import PlayerDecision, PlayerPolicy, parse_player_decision
from simulation_scenario import NPCS, PHASES, phase_index_for_turn, phase_progress


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


def test_phase_mapping_spans_full_campaign():
    assert phase_index_for_turn(1, 1000) == 0
    assert phase_index_for_turn(1000, 1000) == len(PHASES) - 1
    indexes = [phase_index_for_turn(turn, 1000) for turn in range(1, 1001)]
    assert indexes == sorted(indexes)
    assert set(indexes) == set(range(len(PHASES)))


def test_phase_progress_is_bounded():
    for turn in range(1, 101):
        index = phase_index_for_turn(turn, 100)
        assert 0.0 <= phase_progress(turn, 100, index) <= 1.0


def test_player_parser_canonicalizes_active_npc_name():
    decision = parse_player_decision(
        '{"target":"sylvia","mode":"question","intent":"I ask what the rune resembles."}',
        ["Sylvia", "Garrick"],
    )
    assert decision.target == "Sylvia"
    assert decision.mode == "question"


def test_player_policy_rejects_outcomes_and_repetition():
    policy = PlayerPolicy()
    outcome = PlayerDecision(
        target="narrator",
        mode="action",
        intent="I successfully open the gate and reveal the hidden chamber.",
    )
    valid, error = policy.validate(outcome, ["Sylvia"])
    assert valid is False
    assert "outcome" in error

    first = PlayerDecision(
        target="narrator",
        mode="action",
        intent="I inspect the hinges without touching them.",
    )
    valid, _ = policy.validate(first, ["Sylvia"])
    assert valid is True
    policy.remember(first)
    valid, error = policy.validate(first, ["Sylvia"])
    assert valid is False
    assert "repeats" in error


def test_player_policy_rejects_inactive_target():
    policy = PlayerPolicy()
    decision = PlayerDecision(
        target="Thorin",
        mode="dialogue",
        intent='I ask, "Can you read this mark?"',
    )
    valid, error = policy.validate(decision, ["Sylvia", "Garrick"])
    assert valid is False
    assert "not active" in error
