from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.services.performance_telemetry_guard import _render_report
from live_model_contracts import console_runner

CASCADE_WARNING_REQUESTS = 10


@dataclass(frozen=True)
class PlannerTurnDiagnostics:
    turn_index: int
    task_id: str
    planner_stages: int
    planner_requests: int
    planner_wall_seconds: float
    trace: str

    @property
    def repair_cascade(self) -> bool:
        return self.planner_requests >= CASCADE_WARNING_REQUESTS


@dataclass(frozen=True)
class RunDiagnostics:
    case_id: str
    repetition: int
    passed: bool
    turns: tuple[PlannerTurnDiagnostics, ...]
    total_llm_stages: int
    total_llm_requests: int
    failure: str

    @property
    def repair_cascade(self) -> bool:
        return any(turn.repair_cascade for turn in self.turns)

    @property
    def worst_turn_requests(self) -> int:
        return max((turn.planner_requests for turn in self.turns), default=0)


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not path.exists():
        return records
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def _attempt_count(record: dict[str, Any]) -> int:
    value = record.get("attempt_count")
    if isinstance(value, (int, float)) and value > 0:
        return int(value)
    attempts = record.get("attempts")
    if isinstance(attempts, list) and attempts:
        return len(attempts)
    return 1


def _number(record: dict[str, Any], key: str) -> float:
    value = record.get(key)
    return float(value) if isinstance(value, (int, float)) else 0.0


def _stage_label(record: dict[str, Any]) -> str:
    response_model = str(record.get("response_model") or "").strip()
    if response_model:
        return response_model
    stage = str(record.get("stage") or "unknown")
    if ":" in stage:
        stage = stage.split(":", 1)[1]
    return stage


def _collapsed_trace(records: list[dict[str, Any]], *, role: str = "planner") -> str:
    labels: list[tuple[str, int]] = []
    for record in records:
        if role and str(record.get("role") or "") != role:
            continue
        label = _stage_label(record)
        attempts = _attempt_count(record)
        rendered = label if attempts == 1 else f"{label}[x{attempts}]"
        if labels and labels[-1][0] == rendered:
            previous, count = labels[-1]
            labels[-1] = (previous, count + 1)
        else:
            labels.append((rendered, 1))
    parts = [label if count == 1 else f"{label}×{count}" for label, count in labels]
    return " -> ".join(parts) if parts else "-"


def _planner_turns(records: list[dict[str, Any]]) -> tuple[PlannerTurnDiagnostics, ...]:
    """Group Planner work by asyncio task, which is one live-contract turn boundary.

    The live runner executes each user turn under its own timeout task. Performance telemetry records
    that asyncio task id, so grouping by task avoids falsely treating a multi-turn contract as one
    enormous repair cascade.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    first_sequence: dict[str, float] = {}
    for record in records:
        if str(record.get("role") or "") != "planner":
            continue
        task_id = str(record.get("task_id") or "unknown")
        grouped.setdefault(task_id, []).append(record)
        sequence = _number(record, "sequence")
        first_sequence[task_id] = min(first_sequence.get(task_id, sequence), sequence)

    ordered_ids = sorted(grouped, key=lambda task_id: first_sequence.get(task_id, 0.0))
    turns: list[PlannerTurnDiagnostics] = []
    for index, task_id in enumerate(ordered_ids, start=1):
        planner = grouped[task_id]
        turns.append(
            PlannerTurnDiagnostics(
                turn_index=index,
                task_id=task_id,
                planner_stages=len(planner),
                planner_requests=sum(_attempt_count(record) for record in planner),
                planner_wall_seconds=(
                    sum(_number(record, "wall_ms") for record in planner) / 1000.0
                ),
                trace=_collapsed_trace(planner),
            )
        )
    return tuple(turns)


def _case_payload(run_dir: Path, case_id: str, repetition: int) -> dict[str, Any]:
    path = run_dir / "cases" / case_id / f"run-{repetition}.json"
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _child_identity(child_dir: Path) -> tuple[str, int] | None:
    try:
        case_id = child_dir.parent.name
        run_name = child_dir.name
        if not run_name.startswith("run-"):
            return None
        repetition = int(run_name.removeprefix("run-"))
    except (ValueError, AttributeError):
        return None
    return case_id, repetition


def _diagnose_child(run_dir: Path, child_dir: Path) -> tuple[RunDiagnostics, list[dict[str, Any]]] | None:
    identity = _child_identity(child_dir)
    if identity is None:
        return None
    case_id, repetition = identity
    records = _load_jsonl(child_dir / "llm-performance.jsonl")
    payload = _case_payload(run_dir, case_id, repetition)
    failures = payload.get("failures") if isinstance(payload.get("failures"), list) else []
    failure = str(failures[0]).splitlines()[0] if failures else ""
    diagnostic = RunDiagnostics(
        case_id=case_id,
        repetition=repetition,
        passed=bool(payload.get("passed")),
        turns=_planner_turns(records),
        total_llm_stages=len(records),
        total_llm_requests=sum(_attempt_count(record) for record in records),
        failure=failure,
    )
    enriched = [
        {
            "case_id": case_id,
            "repetition": repetition,
            **record,
        }
        for record in records
    ]
    return diagnostic, enriched


def _collect(run_dir: Path) -> tuple[list[RunDiagnostics], list[dict[str, Any]]]:
    diagnostics: list[RunDiagnostics] = []
    records: list[dict[str, Any]] = []
    isolated = run_dir / "isolated"
    if not isolated.exists():
        return diagnostics, records
    for child_dir in sorted(isolated.glob("*/run-*")):
        result = _diagnose_child(run_dir, child_dir)
        if result is None:
            continue
        diagnostic, child_records = result
        diagnostics.append(diagnostic)
        records.extend(child_records)
    return diagnostics, records


def _short(text: str, limit: int = 180) -> str:
    clean = " ".join(text.split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1] + "…"


def _cascade_rows(diagnostics: list[RunDiagnostics]) -> list[tuple[RunDiagnostics, PlannerTurnDiagnostics]]:
    rows = [
        (run, turn)
        for run in diagnostics
        for turn in run.turns
        if turn.repair_cascade
    ]
    return sorted(
        rows,
        key=lambda item: (item[1].planner_requests, item[1].planner_wall_seconds),
        reverse=True,
    )


def _diagnostic_markdown(diagnostics: list[RunDiagnostics]) -> str:
    if not diagnostics:
        return "\n\n## Control-plane cascade diagnostics\n\nNo LLM performance traces were found.\n"
    cascades = _cascade_rows(diagnostics)
    failed_runs = [item for item in diagnostics if not item.passed]
    failed_with_cascade = [item for item in failed_runs if item.repair_cascade]
    total_turns = sum(len(item.turns) for item in diagnostics)
    lines = [
        "",
        "## Control-plane cascade diagnostics",
        "",
        (
            f"Repair-cascade warning threshold: **{CASCADE_WARNING_REQUESTS} planner provider "
            "requests in one user turn**. This is diagnostic only and does not change PASS/FAIL semantics."
        ),
        "",
        f"Turns with repair cascade: **{len(cascades)}/{total_turns}**  ",
        f"Failed contract runs containing a cascade: **{len(failed_with_cascade)}/{len(failed_runs)}**",
        "",
        "| Case | Run | Turn | Result | Planner stages | Planner requests | Planner wall | Trace |",
        "| --- | ---: | ---: | --- | ---: | ---: | ---: | --- |",
    ]
    for run, turn in cascades[:40]:
        result = "PASS" if run.passed else "FAIL"
        lines.append(
            f"| {run.case_id} | {run.repetition} | {turn.turn_index} | {result} ⚠ cascade | "
            f"{turn.planner_stages} | {turn.planner_requests} | "
            f"{turn.planner_wall_seconds:.1f}s | {_short(turn.trace, 280)} |"
        )

    worst_runs = sorted(
        (item for item in failed_runs if item.failure),
        key=lambda item: item.worst_turn_requests,
        reverse=True,
    )
    if worst_runs:
        lines.extend(["", "### Failure endpoints for failed runs", ""])
        for item in worst_runs[:20]:
            lines.append(
                f"- `{item.case_id}` run {item.repetition}: worst turn "
                f"{item.worst_turn_requests} planner requests; {_short(item.failure)}"
            )
    lines.append("")
    return "\n".join(lines)


def _write_aggregate_performance(run_dir: Path, records: list[dict[str, Any]]) -> None:
    if not records:
        return
    raw_path = run_dir / "llm-performance.jsonl"
    raw_path.write_text(
        "".join(json.dumps(record, ensure_ascii=False, default=str) + "\n" for record in records),
        encoding="utf-8",
    )
    (run_dir / "llm-performance.md").write_text(_render_report(records), encoding="utf-8")


def _latest_dir() -> Path:
    backend = Path(__file__).resolve().parents[1]
    return backend / "data" / "live-model-contracts" / "latest"


def _publish_updated_artifacts(run_dir: Path) -> None:
    latest = _latest_dir()
    latest.mkdir(parents=True, exist_ok=True)
    for name in ("report.md", "llm-performance.md", "llm-performance.jsonl"):
        source = run_dir / name
        if source.exists():
            shutil.copy2(source, latest / name)


def _print_diagnostics(diagnostics: list[RunDiagnostics], run_dir: Path) -> None:
    cascades = _cascade_rows(diagnostics)
    total_turns = sum(len(item.turns) for item in diagnostics)
    print("\n=======================================================================")
    print("                 CONTROL-PLANE CASCADE DIAGNOSTICS")
    print("=======================================================================")
    print(
        f"Repair cascades (>= {CASCADE_WARNING_REQUESTS} planner requests in one turn): "
        f"{len(cascades)}/{total_turns}"
    )
    for run, turn in cascades:
        status = "PASS" if run.passed else "FAIL"
        print(
            f"[CASCADE] {status} {run.case_id} run {run.repetition} turn {turn.turn_index}: "
            f"{turn.planner_requests} planner requests in {turn.planner_stages} stages, "
            f"{turn.planner_wall_seconds:.1f}s planner wall"
        )
        print(f"          {_short(turn.trace, 440)}")
    print(f"Aggregate performance: {run_dir / 'llm-performance.md'}")
    print(f"Raw LLM trace:          {run_dir / 'llm-performance.jsonl'}")


def _latest_run_dir() -> Path | None:
    pointer = _latest_dir() / "run-path.txt"
    if not pointer.exists():
        return None
    try:
        path = Path(pointer.read_text(encoding="utf-8").strip()).resolve()
    except OSError:
        return None
    return path if path.exists() else None


def main() -> int:
    rc = console_runner.main()
    run_dir = _latest_run_dir()
    if run_dir is None:
        print("[diagnostics] Aggregate run path was not published; cascade analysis skipped.")
        return rc

    diagnostics, records = _collect(run_dir)
    _write_aggregate_performance(run_dir, records)

    report = run_dir / "report.md"
    if report.exists():
        original = report.read_text(encoding="utf-8", errors="replace")
        report.write_text(
            original.rstrip() + "\n" + _diagnostic_markdown(diagnostics),
            encoding="utf-8",
        )

    _publish_updated_artifacts(run_dir)
    _print_diagnostics(diagnostics, run_dir)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
