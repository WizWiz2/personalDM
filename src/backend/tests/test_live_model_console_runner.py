from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

from live_model_contracts.console_runner import (
    ProgressState,
    _child_command,
    _child_dir,
    _format_duration,
    _load_child_payload,
    _progress_line,
    _publish_latest,
)


def test_duration_format_is_stable_for_long_local_runs() -> None:
    assert _format_duration(None) == "--:--:--"
    assert _format_duration(0) == "00:00:00"
    assert _format_duration(3661) == "01:01:01"


def test_progress_line_reports_count_pass_fail_elapsed_and_eta() -> None:
    state = ProgressState(
        total=4,
        started_at=0.0,
        completed=1,
        passed=1,
        failed=0,
        completed_wall_seconds=[30.0],
    )

    line = _progress_line(state, now=60.0, width=8)

    assert "1/4" in line
    assert "25%" in line
    assert "PASS 1 FAIL 0" in line
    assert "elapsed 00:01:00" in line
    assert "ETA 00:01:30" in line
    assert "[##------]" in line


def _args() -> argparse.Namespace:
    return argparse.Namespace(
        narrator_model="gemma4:e4b",
        control_model="qwen2.5:7b",
        ollama="http://127.0.0.1:11434",
        turn_timeout=180.0,
        post_turn_timeout=120.0,
        control_timeout=90.0,
    )


def test_child_command_forces_one_case_one_repeat_and_unique_output(tmp_path: Path) -> None:
    child = tmp_path / "isolated" / "movement_known_location" / "run-2"

    command = _child_command(_args(), "movement_known_location", child)

    assert command[1:3] == ["-m", "live_model_contracts.runner"]
    assert command[command.index("--case") + 1] == "movement_known_location"
    assert command[command.index("--repeat") + 1] == "1"
    assert Path(command[command.index("--output") + 1]) == child
    assert command[command.index("--narrator-model") + 1] == "gemma4:e4b"
    assert command[command.index("--control-model") + 1] == "qwen2.5:7b"


def test_child_directories_are_isolated_by_case_and_repetition(tmp_path: Path) -> None:
    one = _child_dir(tmp_path, "movement_known_location", 1)
    two = _child_dir(tmp_path, "movement_known_location", 2)
    other = _child_dir(tmp_path, "item_drop", 1)

    assert len({one, two, other}) == 3
    assert one.name == "run-1"
    assert two.name == "run-2"
    assert other.parent.name == "item_drop"


def test_child_result_is_relabelled_to_aggregate_repetition(tmp_path: Path) -> None:
    case = SimpleNamespace(
        id="movement_known_location",
        title="movement",
        transitions=("movement",),
    )
    child = tmp_path / "child"
    result_path = child / "cases" / case.id / "run-1.json"
    result_path.parent.mkdir(parents=True)
    result_path.write_text(
        json.dumps(
            {
                "case_id": case.id,
                "title": case.title,
                "transitions": ["movement"],
                "repetition": 1,
                "passed": True,
                "failures": [],
                "turns": [],
                "elapsed_seconds": 10.0,
                "before": {},
                "after": {},
                "delta": {},
            }
        ),
        encoding="utf-8",
    )

    payload = _load_child_payload(child, case, repetition=2, worker_exit_code=0, wall_seconds=11.0)

    assert payload["passed"] is True
    assert payload["repetition"] == 2


def test_worker_crash_becomes_case_failure_instead_of_aborting_suite(tmp_path: Path) -> None:
    case = SimpleNamespace(
        id="movement_known_location",
        title="movement",
        transitions=("movement",),
    )
    child = tmp_path / "child"
    child.mkdir()
    (child / "child.log").write_text("sqlite3.OperationalError: database is locked\n", encoding="utf-8")

    payload = _load_child_payload(child, case, repetition=1, worker_exit_code=1, wall_seconds=5.0)

    assert payload["passed"] is False
    assert payload["repetition"] == 1
    assert "worker exited with code 1" in payload["failures"][0]
    assert "database is locked" in payload["failures"][1]


def test_latest_report_keeps_stable_pointer_to_timestamped_run(tmp_path: Path) -> None:
    backend = tmp_path / "backend"
    run_dir = backend / "data" / "live-model-contracts" / "20260903T000000Z"
    run_dir.mkdir(parents=True)
    (run_dir / "report.md").write_text("# report\n", encoding="utf-8")
    (run_dir / "manifest.json").write_text('{"overall": true}\n', encoding="utf-8")

    latest = _publish_latest(run_dir, backend)

    assert latest == backend / "data" / "live-model-contracts" / "latest"
    assert (latest / "report.md").read_text(encoding="utf-8") == "# report\n"
    assert (latest / "manifest.json").read_text(encoding="utf-8") == '{"overall": true}\n'
    assert Path((latest / "run-path.txt").read_text(encoding="utf-8").strip()) == run_dir.resolve()
