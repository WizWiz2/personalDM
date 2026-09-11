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
class RunDiagnostics:
    case_id: str
    repetition: int
    passed: bool
    planner_stages: int
    planner_requests: int
    planner_wall_seconds: float
    total_llm_stages: int
    total_llm_requests: int
    trace: str
    failure: str

    @property
    def repair_cascade(self) -> bool:
        return self.planner_requests >= CASCADE_WARNING_REQUESTS


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
    planner = [record for record in records if str(record.get("role") or "") == "planner"]
    failures = payload.get("failures") if isinstance(payload.get("failures"), list) else []
    failure = str(failures[0]).splitlines()[0] if failures else ""
    diagnostic = RunDiagnostics(
        case_id=case_id,
        repetition=repetition,
        passed=bool(payload.get("passed")),
        planner_stages=len(planner),
        planner_requests=sum(_attempt_count(record) for record in planner),
        planner_wall_seconds=sum(_number(record, "wall_ms") for record in planner) / 1000.0,
        total_llm_stages=len(records),
        total_llm_requests=sum(_attempt_count(record) for record in records),
        trace=_collapsed_trace(records),
        failure=failure,
    )
    enriched: list[dict[str, Any]] = []
    for record in records:
        enriched.append(
            {
                "case_id": case_id,
                "repetition": repetition,
                **record,
            }
        )
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


def _diagnostic_markdown(diagnostics: list[RunDiagnostics]) -> str:
    if not diagnostics:
        return "\n\n## Control-plane cascade diagnostics\n\nNo LLM performance traces were found.\n"
    cascades = [item for item in diagnostics if item.repair_cascade]
    failed = [item for item in diagnostics if not item.passed]
    ordered = sorted(
        diagnostics,
        key=lambda item: (item.planner_requests, item.planner_wall_seconds),
        reverse=True,
    )
    lines = [
        "",
        "## Control-plane cascade diagnostics",
        "",
        (
            f"Repair-cascade warning threshold: **{CASCADE_WARNING_REQUESTS} planner provider "
            "requests per isolated contract run**. This is diagnostic only and does not change "
            "PASS/FAIL semantics."
        ),
        "",
        f"Runs with repair cascade: **{len(cascades)}/{len(diagnostics)}**  ",
        f"Failed runs with repair cascade: **{sum(1 for item in failed if item.repair_cascade)}/{len(failed)}**",
        "",
        "| Case | Run | Result | Planner stages | Planner requests | Planner wall | Trace |",
        "| --- | ---: | --- | ---: | ---: | ---: | --- |",
    ]
    for item in ordered[:30]:
        result = "PASS" if item.passed else "FAIL"
        cascade = " ⚠ cascade" if item.repair_cascade else ""
        lines.append(
            f"| {item.case_id} | {item.repetition} | {result}{cascade} | "
            f"{item.planner_stages} | {item.planner_requests} | "
            f"{item.planner_wall_seconds:.1f}s | {_short(item.trace, 260)} |"
        )
    worst_failures = [item for item in ordered if not item.passed and item.failure]
    if worst_failures:
        lines.extend(["", "### Failure endpoints for the worst cascades", ""])
        for item in worst_failures[:15]:
            lines.append(
                f"- `{item.case_id}` run {item.repetition}: {item.planner_requests} planner requests; "
                f"{_short(item.failure)}"
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


def _publish_updated_artifacts(run_dir: Path) -> None:
    latest = run_dir.parent / "latest"
    if not latest.exists():
        return
    for name in ("report.md", "llm-performance.md", "llm-performance.jsonl"):
        source = run_dir / name
        if source.exists():
            shutil.copy2(source, latest / name)


def _print_diagnostics(diagnostics: list[RunDiagnostics], run_dir: Path) -> None:
    cascades = sorted(
        (item for item in diagnostics if item.repair_cascade),
        key=lambda item: item.planner_requests,
        reverse=True,
    )
    print("\n=======================================================================")
    print("                 CONTROL-PLANE CASCADE DIAGNOSTICS")
    print("=======================================================================")
    print(
        f"Repair cascades (>= {CASCADE_WARNING_REQUESTS} planner requests): "
        f"{len(cascades)}/{len(diagnostics)}"
    )
    for item in cascades:
        status = "PASS" if item.passed else "FAIL"
        print(
            f"[CASCADE] {status} {item.case_id} run {item.repetition}: "
            f"{item.planner_requests} planner requests in {item.planner_stages} stages, "
            f"{item.planner_wall_seconds:.1f}s planner wall"
        )
        print(f"          {_short(item.trace, 420)}")
    print(f"Aggregate performance: {run_dir / 'llm-performance.md'}")
    print(f"Raw LLM trace:          {run_dir / 'llm-performance.jsonl'}")


def _latest_run_dir() -> Path | None:
    backend = Path(__file__).resolve().parents[1]
    pointer = backend / "data" / "live-model-contracts" / "latest" / "run-path.txt"
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
        report.write_text(original.rstrip() + "\n" + _diagnostic_markdown(diagnostics), encoding="utf-8")

    _publish_updated_artifacts(run_dir)
    _print_diagnostics(diagnostics, run_dir)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
