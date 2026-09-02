from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# IMPORTANT: do not import ``app`` at module import time. The isolated database/model env must be
# installed before PersonalDM settings and SQLAlchemy engine are created.


@dataclass
class TurnRun:
    input: str
    status: str
    error: str | None
    latency_seconds: float


@dataclass
class CaseRun:
    case_id: str
    title: str
    transitions: tuple[str, ...]
    repetition: int
    passed: bool
    failures: list[str]
    turns: list[TurnRun]
    elapsed_seconds: float
    before: dict[str, Any]
    after: dict[str, Any]
    delta: dict[str, Any]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run deterministic truth-state contracts through the real local PersonalDM model stack. "
            "This command is intentionally not part of GitHub Actions."
        )
    )
    parser.add_argument("--narrator-model", default="gemma4:e4b")
    parser.add_argument("--control-model", default="qwen2.5:7b")
    parser.add_argument("--ollama", default="http://127.0.0.1:11434")
    parser.add_argument("--suite", choices=("core", "extended", "all"), default="core")
    parser.add_argument("--case", action="append", dest="cases", help="Run only this case id; repeat the flag for several cases")
    parser.add_argument("--repeat", type=int, default=1, help="Repeat each case in a fresh campaign")
    parser.add_argument("--turn-timeout", type=float, default=180.0)
    parser.add_argument("--post-turn-timeout", type=float, default=120.0)
    parser.add_argument("--control-timeout", type=float, default=90.0)
    parser.add_argument("--list", action="store_true", help="List contracts without running models")
    parser.add_argument("--output", type=Path, default=None, help="Artifact directory; defaults to data/live-model-contracts/<timestamp>")
    return parser.parse_args()


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _api_base(ollama: str) -> str:
    return ollama.rstrip("/") + "/v1"


def _ollama_models(ollama: str) -> set[str]:
    url = ollama.rstrip("/") + "/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 - explicit local endpoint
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Ollama is not reachable at {url}. Start Ollama before live-model contracts: {exc}"
        ) from exc
    result: set[str] = set()
    for item in payload.get("models") or []:
        for key in ("name", "model"):
            value = str(item.get(key) or "").strip()
            if value:
                result.add(value)
    return result


def _model_available(requested: str, available: set[str]) -> bool:
    if requested in available:
        return True
    requested_base = requested.split(":", 1)[0]
    return any(value.split(":", 1)[0] == requested_base for value in available)


def _install_isolated_env(args: argparse.Namespace, run_dir: Path) -> Path:
    db_path = (run_dir / "live-contracts.db").resolve()
    data_dir = (run_dir / "runtime-data").resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    base_url = _api_base(args.ollama)

    values = {
        "PDM_DATA_DIR": str(data_dir),
        "PDM_DATABASE_URL": "sqlite+aiosqlite:///" + db_path.as_posix(),
        "PDM_TEXT_PROVIDER": "local",
        "PDM_LLM_BASE_URL": base_url,
        "PDM_LLM_MODEL": args.narrator_model,
        "PDM_CONTROL_LLM_BASE_URL": base_url,
        "PDM_CONTROL_LLM_MODEL": args.control_model,
        "PDM_PLANNER_LLM_MODEL": args.control_model,
        "PDM_NARRATION_VALIDATOR_LLM_MODEL": args.control_model,
        "PDM_SCRIBE_LLM_MODEL": args.control_model,
        "PDM_CURATOR_LLM_MODEL": args.control_model,
        "PDM_EVALUATOR_LLM_MODEL": args.control_model,
        "PDM_PLAYER_LLM_MODEL": args.control_model,
        "PDM_SCENARIO_BUILDER_LLM_MODEL": args.control_model,
        "PDM_CONTROL_LLM_TIMEOUT_SECONDS": str(args.control_timeout),
        "PDM_IMAGE_PROVIDER": "off",
        "PDM_IMAGE_ENABLED": "false",
        # Never allow a cloud credential from the user's normal .env to change the local test route.
        "PDM_LLM_API_KEY": "",
        "PDM_CONTROL_LLM_API_KEY": "",
    }
    os.environ.update(values)
    return db_path


def _migrate(backend: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=backend,
        env=os.environ.copy(),
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "Could not initialize isolated live-contract database:\n"
            + result.stdout[-4000:]
            + "\n"
            + result.stderr[-4000:]
        )


def _wait_generation(client, campaign_id: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    latest: dict | None = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/campaigns/{campaign_id}/turns/generation/latest")
        if response.status_code != 200:
            raise RuntimeError(f"generation poll failed: {response.status_code} {response.text}")
        latest = response.json()
        if latest and latest.get("status") in {"completed", "failed", "cancelled"}:
            return latest
        time.sleep(0.25)
    raise TimeoutError(f"generation did not finish within {timeout:g}s; latest={latest}")


def _wait_post_turn(db_path: Path, campaign_id: str, timeout: float) -> None:
    import sqlite3

    deadline = time.monotonic() + timeout
    last: list[tuple] = []
    while time.monotonic() < deadline:
        db = sqlite3.connect(db_path)
        try:
            last = db.execute(
                "SELECT job_type, status, attempts, error FROM post_turn_jobs WHERE campaign_id=? ORDER BY created_at",
                (campaign_id,),
            ).fetchall()
        finally:
            db.close()
        if not last or all(row[1] in {"completed", "failed", "skipped", "cancelled"} for row in last):
            return
        time.sleep(0.25)
    raise TimeoutError(f"post-turn jobs did not settle within {timeout:g}s: {last}")


def _run_turn(client, db_path: Path, campaign_id: str, text: str, args: argparse.Namespace) -> TurnRun:
    started = time.monotonic()
    response = client.post(
        f"/api/campaigns/{campaign_id}/turns/async",
        json={"role": "user", "content": text},
    )
    if response.status_code != 202:
        return TurnRun(
            input=text,
            status="submit_failed",
            error=f"HTTP {response.status_code}: {response.text[:2000]}",
            latency_seconds=round(time.monotonic() - started, 3),
        )
    try:
        generation = _wait_generation(client, campaign_id, args.turn_timeout)
        if generation["status"] == "completed":
            _wait_post_turn(db_path, campaign_id, args.post_turn_timeout)
        return TurnRun(
            input=text,
            status=str(generation["status"]),
            error=generation.get("error"),
            latency_seconds=round(time.monotonic() - started, 3),
        )
    except (RuntimeError, TimeoutError) as exc:
        return TurnRun(
            input=text,
            status="timeout_or_poll_failure",
            error=str(exc),
            latency_seconds=round(time.monotonic() - started, 3),
        )


def _select_cases(args: argparse.Namespace):
    from live_model_contracts.cases import all_cases

    cases = list(all_cases())
    if args.cases:
        requested = set(args.cases)
        unknown = requested - {case.id for case in cases}
        if unknown:
            raise RuntimeError(f"Unknown live-model case(s): {', '.join(sorted(unknown))}")
        cases = [case for case in cases if case.id in requested]
    elif args.suite == "core":
        cases = [case for case in cases if case.suite == "core"]
    elif args.suite == "extended":
        cases = [case for case in cases if case.suite == "extended"]
    return cases


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _run_case(client, db_path: Path, case, repetition: int, args: argparse.Namespace) -> CaseRun:
    from live_model_contracts.snapshot import capture, semantic_diff
    from live_model_contracts.world import build_standard_world

    started = time.monotonic()
    world = build_standard_world(
        client,
        narrator_base_url=_api_base(args.ollama),
        narrator_model=args.narrator_model,
    )
    if case.prepare:
        case.prepare(client, world)
    before = capture(db_path, world.campaign_id)

    turn_runs: list[TurnRun] = []
    failures: list[str] = []
    for text in case.turns:
        turn = _run_turn(client, db_path, world.campaign_id, text, args)
        turn_runs.append(turn)
        if turn.status != "completed":
            failures.append(f"turn failed [{turn.status}]: {turn.error}")
            break

    if not failures and case.finalize:
        try:
            case.finalize(client, world)
        except Exception as exc:  # noqa: BLE001 - diagnostic boundary
            failures.append(f"finalize action failed: {exc}")

    after = capture(db_path, world.campaign_id)
    if not failures:
        try:
            failures.extend(case.oracle(before, after, world))
        except Exception as exc:  # noqa: BLE001 - oracle should become test evidence, not abort suite
            failures.append(f"oracle crashed: {type(exc).__name__}: {exc}")

    return CaseRun(
        case_id=case.id,
        title=case.title,
        transitions=case.transitions,
        repetition=repetition,
        passed=not failures,
        failures=failures,
        turns=turn_runs,
        elapsed_seconds=round(time.monotonic() - started, 3),
        before=before.to_json(),
        after=after.to_json(),
        delta=semantic_diff(before, after),
    )


def _render_summary(runs: list[CaseRun], case_specs, args: argparse.Namespace) -> tuple[str, bool]:
    by_case: dict[str, list[CaseRun]] = {}
    for run in runs:
        by_case.setdefault(run.case_id, []).append(run)
    specs = {case.id: case for case in case_specs}
    lines = [
        "# PersonalDM live model contracts",
        "",
        f"Narrator: `{args.narrator_model}`",
        f"Control: `{args.control_model}`",
        f"Repeat: {args.repeat}",
        "",
        "| Contract | Pass | Required | Time |",
        "| --- | ---: | ---: | ---: |",
    ]
    overall = True
    for case_id, case_runs in by_case.items():
        passed = sum(run.passed for run in case_runs)
        rate = passed / len(case_runs)
        required = specs[case_id].min_pass_rate
        total_time = sum(run.elapsed_seconds for run in case_runs)
        ok = rate + 1e-9 >= required
        overall = overall and ok
        lines.append(
            f"| {case_id} | {passed}/{len(case_runs)} ({rate:.0%}) | {required:.0%} | {total_time:.1f}s |"
        )
    lines.extend(["", f"Overall: **{'PASS' if overall else 'FAIL'}**", ""])
    failed_runs = [run for run in runs if not run.passed]
    if failed_runs:
        lines.extend(["## Failures", ""])
        for run in failed_runs:
            lines.append(f"### {run.case_id} / repetition {run.repetition}")
            for failure in run.failures:
                lines.append(f"- {failure}")
            lines.append("")
    return "\n".join(lines), overall


def main() -> int:
    args = _parse_args()
    if args.repeat < 1:
        raise SystemExit("--repeat must be >= 1")

    # The registry is safe to import before PersonalDM; it contains no app imports at module scope.
    case_specs = _select_cases(args)
    if args.list:
        for case in case_specs:
            print(f"{case.id:36} [{case.suite}] {', '.join(case.transitions)}")
        return 0

    available = _ollama_models(args.ollama)
    missing = [
        model
        for model in (args.narrator_model, args.control_model)
        if not _model_available(model, available)
    ]
    if missing:
        print("Missing Ollama model(s): " + ", ".join(missing), file=sys.stderr)
        print("Available: " + ", ".join(sorted(available)), file=sys.stderr)
        return 2

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backend = _backend_root()
    run_dir = (args.output or backend / "data" / "live-model-contracts" / timestamp).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    db_path = _install_isolated_env(args, run_dir)
    _migrate(backend)

    # Only now may the application be imported.
    from live_model_contracts.world import new_client

    runs: list[CaseRun] = []
    with new_client() as client:
        for case in case_specs:
            for repetition in range(1, args.repeat + 1):
                print(f"[{case.id}] run {repetition}/{args.repeat} ...", flush=True)
                run = _run_case(client, db_path, case, repetition, args)
                runs.append(run)
                status = "PASS" if run.passed else "FAIL"
                latency = sum(item.latency_seconds for item in run.turns)
                print(f"[{case.id}] {status} ({latency:.1f}s model time)", flush=True)
                case_dir = run_dir / "cases" / case.id
                _write_json(case_dir / f"run-{repetition}.json", asdict(run))

    summary, overall = _render_summary(runs, case_specs, args)
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
        },
    )
    print("\n" + summary)
    print(f"Artifacts: {run_dir}")
    return 0 if overall else 1


if __name__ == "__main__":
    raise SystemExit(main())
