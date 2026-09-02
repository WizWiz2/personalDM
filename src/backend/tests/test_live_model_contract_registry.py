from pathlib import Path

from live_model_contracts.cases import all_cases


def test_live_model_contract_ids_and_transition_ownership_are_explicit():
    cases = list(all_cases())
    ids = [case.id for case in cases]

    assert len(cases) >= 16
    assert len(ids) == len(set(ids))
    assert all(case.transitions for case in cases)
    assert all(case.turns and all(turn.strip() for turn in case.turns) for case in cases)

    required_transition_families = {
        "scene",
        "movement",
        "location",
        "presence",
        "character",
        "item",
        "time",
        "knowledge",
        "fact",
        "relationship",
        "thesis",
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


def test_live_runner_is_not_allowed_to_import_pytest_mocks_or_call_itself_a_simulation():
    package = Path(__file__).resolve().parents[1] / "live_model_contracts"
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in package.glob("*.py")
    ).casefold()

    assert "unittest.mock" not in source
    assert "tests.conftest" not in source
    assert "run_ci_mock_simulation" not in source


def test_truth_oracle_contains_no_llm_grader():
    package = Path(__file__).resolve().parents[1] / "live_model_contracts"
    oracle = (package / "oracle_snapshot.py").read_text(encoding="utf-8").casefold()

    assert "llmprovider" not in oracle
    assert "rolemodelrouter" not in oracle
    assert "generate_json" not in oracle
    assert "generate_stream" not in oracle
