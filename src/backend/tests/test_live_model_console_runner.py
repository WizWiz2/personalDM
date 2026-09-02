from __future__ import annotations

from pathlib import Path

from live_model_contracts.console_runner import (
    ProgressState,
    _DONE_RE,
    _START_RE,
    _format_duration,
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


def test_progress_patterns_recognize_runner_contract_boundaries() -> None:
    started = _START_RE.match("[movement_known_location] run 1/2 ...")
    finished = _DONE_RE.match("[movement_known_location] PASS (87.4s model time)")

    assert started is not None
    assert started.group("case") == "movement_known_location"
    assert started.group("repeat") == "1"
    assert finished is not None
    assert finished.group("status") == "PASS"
    assert finished.group("model") == "87.4"


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
