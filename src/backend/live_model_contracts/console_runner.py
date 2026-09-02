from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass
class ProgressState:
    total: int
    started_at: float
    completed: int = 0
    passed: int = 0
    failed: int = 0
    completed_wall_seconds: list[float] = field(default_factory=list)
    current_case: str | None = None
    current_repeat: str | None = None
    current_started_at: float | None = None


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _format_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "--:--:--"
    total = round(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _progress_line(state: ProgressState, now: float | None = None, width: int = 24) -> str:
    now = time.monotonic() if now is None else now
    ratio = state.completed / state.total if state.total else 1.0
    filled = min(width, max(0, round(width * ratio)))
    bar = "#" * filled + "-" * (width - filled)
    elapsed = now - state.started_at
    eta: float | None = None
    if state.completed_wall_seconds and state.completed < state.total:
        average = sum(state.completed_wall_seconds) / len(state.completed_wall_seconds)
        eta = average * (state.total - state.completed)
    elif state.completed >= state.total:
        eta = 0.0
    current = ""
    if state.current_case and state.current_started_at is not None:
        current_elapsed = max(0.0, now - state.current_started_at)
        current = f" | current {state.current_case} {_format_duration(current_elapsed)}"
    return (
        f"[{bar}] {state.completed}/{state.total} {ratio:>5.0%}"
        f" | PASS {state.passed} FAIL {state.failed}"
        f" | elapsed {_format_duration(elapsed)} | ETA {_format_duration(eta)}{current}"
    )


def _publish_latest(run_dir: Path, backend: Path) -> Path | None:
    report = run_dir / "report.md"
    manifest = run_dir / "manifest.json"
    if not report.exists() or not manifest.exists():
        return None
    latest = backend / "data" / "live-model-contracts" / "latest"
    latest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(report, latest / "report.md")
    shutil.copy2(manifest, latest / "manifest.json")
    (latest / "run-path.txt").write_text(str(run_dir.resolve()) + os.linesep, encoding="utf-8")
    return latest


def _clear_progress(last_width: int) -> None:
    if last_width:
        print("\r" + " " * last_width + "\r", end="", flush=True)


def _selection():
    from live_model_contracts import runner

    args = runner._parse_args()
    cases = list(runner._select_cases(args))
    return runner, args, cases


def _run_dir(args, backend: Path) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return (args.output or backend / "data" / "live-model-contracts" / timestamp).resolve()


def _child_dir(run_dir: Path, case_id: str, repetition: int) -> Path:
    return run_dir / "isolated" / case_id / f"run-{repetition}"


def _child_command(args, case_id: str, output: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "live_model_contracts.runner",
        "--narrator-model",
        args.narrator_model,
        "--control-model",
        args.control_model,
        "--ollama",
        args.ollama,
        "--suite",
        "all",
        "--case",
        case_id,
        "--repeat",
        "1",
        "--turn-timeout",
        str(args.turn_timeout),
        "--post-turn-timeout",
        str(args.post_turn_timeout),
        "--control-timeout",
        str(args.control_timeout),
        "--output",
        str(output),
    ]


def _tail(path: Path, limit: int = 3500) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    return text[-limit:].strip()


def _load_child_payload(
    child_dir: Path,
    case,
    repetition: int,
    worker_exit_code: int,
    wall_seconds: float,
) -> dict[str, Any]:
    result_path = child_dir / "cases" / case.id / "run-1.json"
    if result_path.exists():
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        payload["repetition"] = repetition
        return payload

    log_path = child_dir / "child.log"
    tail = _tail(log_path)
    failures = [
        f"isolated worker exited with code {worker_exit_code} before producing a case result; "
        f"see {log_path}",
    ]
    if tail:
        failures.append("worker log tail:\n" + tail)
    return {
        "case_id": case.id,
        "title": case.title,
        "transitions": list(case.transitions),
        "repetition": repetition,
        "passed": False,
        "failures": failures,
        "turns": [],
        "elapsed_seconds": round(wall_seconds, 3),
        "before": {},
        "after": {},
        "delta": {},
    }


def _to_case_run(runner, payload: dict[str, Any]):
    return runner.CaseRun(
        case_id=str(payload["case_id"]),
        title=str(payload["title"]),
        transitions=tuple(payload.get("transitions") or ()),
        repetition=int(payload["repetition"]),
        passed=bool(payload["passed"]),
        failures=[str(value) for value in payload.get("failures") or []],
        turns=[],
        elapsed_seconds=float(payload.get("elapsed_seconds") or 0.0),
        before=dict(payload.get("before") or {}),
        after=dict(payload.get("after") or {}),
        delta=dict(payload.get("delta") or {}),
    )


def _model_seconds(payload: dict[str, Any]) -> float:
    return sum(float(turn.get("latency_seconds") or 0.0) for turn in payload.get("turns") or [])


def _run_worker(command: list[str], child_dir: Path, render) -> tuple[int, float]:
    child_dir.mkdir(parents=True, exist_ok=True)
    log_path = child_dir / "child.log"
    started = time.monotonic()
    with log_path.open("w", encoding="utf-8", errors="replace") as log:
        process = subprocess.Popen(
            command,
            cwd=_backend_root(),
            env=os.environ.copy(),
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        while process.poll() is None:
            render()
            time.sleep(1.0)
        return_code = process.wait()
    return return_code, time.monotonic() - started


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    runner, args, case_specs = _selection()
    if args.repeat < 1:
        print("--repeat must be >= 1", file=sys.stderr)
        return 2

    if args.list:
        for case in case_specs:
            print(f"{case.id:36} [{case.suite}] {', '.join(case.transitions)}")
        return 0

    total = len(case_specs) * args.repeat
    if total <= 0:
        print("No live-model contracts selected.", file=sys.stderr)
        return 2

    backend = _backend_root()
    run_dir = _run_dir(args, backend)
    run_dir.mkdir(parents=True, exist_ok=True)

    print(f"Selected contract runs: {total}", flush=True)
    print("Isolation: one Python process + one SQLite database per contract run", flush=True)
    print(f"Aggregate artifacts: {run_dir}", flush=True)

    state = ProgressState(total=total, started_at=time.monotonic())
    last_width = 0
    runs = []

    def render() -> None:
        nonlocal last_width
        line = _progress_line(state)
        print("\r" + line, end="", flush=True)
        last_width = max(last_width, len(line))

    render()
    for case in case_specs:
        for repetition in range(1, args.repeat + 1):
            index = state.completed + 1
            _clear_progress(last_width)
            print(f"[{index:02d}/{total:02d}] {case.id} (run {repetition}) ...", flush=True)
            state.current_case = case.id
            state.current_repeat = str(repetition)
            state.current_started_at = time.monotonic()
            render()

            child_dir = _child_dir(run_dir, case.id, repetition)
            command = _child_command(args, case.id, child_dir)
            worker_exit_code, wall = _run_worker(command, child_dir, render)
            payload = _load_child_payload(child_dir, case, repetition, worker_exit_code, wall)
            run = _to_case_run(runner, payload)
            runs.append(run)

            aggregate_case = run_dir / "cases" / case.id / f"run-{repetition}.json"
            _write_json(aggregate_case, payload)

            state.completed += 1
            state.completed_wall_seconds.append(max(0.0, wall))
            if run.passed:
                state.passed += 1
            else:
                state.failed += 1

            model_seconds = _model_seconds(payload)
            _clear_progress(last_width)
            status = "PASS" if run.passed else "FAIL"
            print(
                f"[{state.completed:02d}/{total:02d}] {status} {case.id} "
                f"({model_seconds:.1f}s model, {wall:.1f}s wall)",
                flush=True,
            )
            if not run.passed:
                for failure in run.failures:
                    first_line = failure.splitlines()[0]
                    print(f"  - {first_line}", flush=True)

            state.current_case = None
            state.current_repeat = None
            state.current_started_at = None
            render()

    _clear_progress(last_width)
    print(_progress_line(state), flush=True)

    summary, overall = runner._render_summary(runs, case_specs, args)
    (run_dir / "report.md").write_text(summary, encoding="utf-8")
    _write_json(
        run_dir / "manifest.json",
        {
            "narrator_model": args.narrator_model,
            "control_model": args.control_model,
            "ollama": args.ollama,
            "suite": args.suite,
            "repeat": args.repeat,
            "cases": [case.id for case in case_specs],
            "overall": overall,
            "isolation": "per_contract_process_and_sqlite",
        },
    )

    print("\n" + summary)
    latest = _publish_latest(run_dir, backend)
    if latest is not None:
        print(f"Latest report: {latest / 'report.md'}")
        print(f"Latest manifest: {latest / 'manifest.json'}")
    print(f"Full artifacts: {run_dir}")
    return 0 if overall else 1


if __name__ == "__main__":
    raise SystemExit(main())
