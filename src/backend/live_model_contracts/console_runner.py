from __future__ import annotations

import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

_START_RE = re.compile(r"^\[(?P<case>[^]]+)] run (?P<repeat>\d+)/(\d+) \.\.\.$")
_DONE_RE = re.compile(
    r"^\[(?P<case>[^]]+)] (?P<status>PASS|FAIL) \((?P<model>[0-9.]+)s model time\)$"
)
_ARTIFACTS_RE = re.compile(r"^Artifacts: (?P<path>.+)$")


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


def _selected_total() -> int:
    from live_model_contracts import runner

    args = runner._parse_args()
    cases = runner._select_cases(args)
    return len(cases) * args.repeat


def _clear_progress(last_width: int) -> None:
    if last_width:
        print("\r" + " " * last_width + "\r", end="", flush=True)


def _run_child(total: int) -> tuple[int, Path | None]:
    command = [sys.executable, "-m", "live_model_contracts.runner", *sys.argv[1:]]
    process = subprocess.Popen(
        command,
        cwd=_backend_root(),
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    assert process.stdout is not None

    lines: queue.Queue[str | None] = queue.Queue()

    def read_output() -> None:
        for line in process.stdout:
            lines.put(line.rstrip("\r\n"))
        lines.put(None)

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()

    state = ProgressState(total=total, started_at=time.monotonic())
    last_width = 0
    artifacts: Path | None = None
    stream_done = False

    def render() -> None:
        nonlocal last_width
        line = _progress_line(state)
        print("\r" + line, end="", flush=True)
        last_width = max(last_width, len(line))

    render()
    while not stream_done:
        try:
            line = lines.get(timeout=1.0)
        except queue.Empty:
            render()
            continue
        if line is None:
            stream_done = True
            break

        start_match = _START_RE.match(line)
        if start_match:
            _clear_progress(last_width)
            state.current_case = start_match.group("case")
            state.current_repeat = start_match.group("repeat")
            state.current_started_at = time.monotonic()
            index = min(state.completed + 1, state.total)
            print(
                f"[{index:02d}/{state.total:02d}] {state.current_case} "
                f"(run {state.current_repeat}) ...",
                flush=True,
            )
            render()
            continue

        done_match = _DONE_RE.match(line)
        if done_match:
            now = time.monotonic()
            wall = (
                now - state.current_started_at
                if state.current_started_at is not None
                else float(done_match.group("model"))
            )
            state.completed += 1
            state.completed_wall_seconds.append(max(0.0, wall))
            if done_match.group("status") == "PASS":
                state.passed += 1
            else:
                state.failed += 1
            _clear_progress(last_width)
            print(
                f"[{state.completed:02d}/{state.total:02d}] {done_match.group('status')} "
                f"{done_match.group('case')} "
                f"({done_match.group('model')}s model, {wall:.1f}s wall)",
                flush=True,
            )
            state.current_case = None
            state.current_repeat = None
            state.current_started_at = None
            render()
            continue

        artifacts_match = _ARTIFACTS_RE.match(line)
        if artifacts_match:
            artifacts = Path(artifacts_match.group("path")).resolve()

        _clear_progress(last_width)
        print(line, flush=True)
        render()

    return_code = process.wait()
    _clear_progress(last_width)
    print(_progress_line(state, width=24), flush=True)
    return return_code, artifacts


def main() -> int:
    total = _selected_total()
    if total <= 0:
        print("No live-model contracts selected.", file=sys.stderr)
        return 2

    print(f"Selected contract runs: {total}", flush=True)
    return_code, artifacts = _run_child(total)

    if artifacts is not None:
        latest = _publish_latest(artifacts, _backend_root())
        if latest is not None:
            print(f"Latest report: {latest / 'report.md'}")
            print(f"Latest manifest: {latest / 'manifest.json'}")
            print(f"Full artifacts: {artifacts}")
        else:
            print(f"Full artifacts: {artifacts}")
            print("Latest report was not updated because this run did not produce report.md/manifest.json.")
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
