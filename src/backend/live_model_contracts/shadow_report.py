from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any


SHADOW_KEY = "te2_semantic_shadow"


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Collect read-only TE2 semantic shadow envelopes beside legacy Scribe proposals "
            "from isolated live-model contract databases."
        )
    )
    parser.add_argument(
        "--run",
        type=Path,
        default=None,
        help="Live-contract aggregate run directory. Defaults to data/live-model-contracts/latest/run-path.txt.",
    )
    return parser.parse_args()


def _resolve_run_dir(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit.resolve()
    pointer = _backend_root() / "data" / "live-model-contracts" / "latest" / "run-path.txt"
    if not pointer.exists():
        raise RuntimeError(f"latest live-contract run pointer does not exist: {pointer}")
    text = pointer.read_text(encoding="utf-8").strip()
    if not text:
        raise RuntimeError(f"latest live-contract run pointer is empty: {pointer}")
    return Path(text).resolve()


def _loads(raw: object, default: Any) -> Any:
    if raw in (None, ""):
        return default
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return value


def _case_identity(db_path: Path, run_dir: Path) -> tuple[str, str]:
    try:
        relative = db_path.relative_to(run_dir)
    except ValueError:
        return "unknown", "unknown"
    parts = relative.parts
    if len(parts) >= 4 and parts[0] == "isolated":
        return parts[1], parts[2]
    return "unknown", "unknown"


def collect_run(run_dir: Path) -> dict[str, Any]:
    databases = sorted((run_dir / "isolated").glob("*/run-*/live-contracts.db"))
    cases: list[dict[str, Any]] = []
    total_assistant_turns = 0
    shadow_turns = 0

    for db_path in databases:
        case_id, repetition = _case_identity(db_path, run_dir)
        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row
        try:
            turns = db.execute(
                """SELECT id, parent_turn_id, content, context_snapshot, status
                     FROM turns
                    WHERE role='assistant'
                    ORDER BY created_at, id"""
            ).fetchall()
            entries: list[dict[str, Any]] = []
            for turn in turns:
                total_assistant_turns += 1
                snapshot = _loads(turn["context_snapshot"], {})
                shadow = snapshot.get(SHADOW_KEY) if isinstance(snapshot, dict) else None
                if not isinstance(shadow, dict):
                    continue
                shadow_turns += 1
                proposals = []
                for proposal in db.execute(
                    """SELECT change_type, payload, status, user_edit
                         FROM proposed_changes
                        WHERE turn_id=? ORDER BY created_at, id""",
                    (turn["id"],),
                ).fetchall():
                    proposals.append(
                        {
                            "change_type": proposal["change_type"],
                            "status": proposal["status"],
                            "payload": _loads(proposal["payload"], proposal["payload"]),
                            "user_edit": _loads(proposal["user_edit"], proposal["user_edit"]),
                        }
                    )
                entries.append(
                    {
                        "assistant_turn_id": turn["id"],
                        "parent_user_turn_id": turn["parent_turn_id"],
                        "turn_status": turn["status"],
                        "assistant_content": turn["content"],
                        "te2_shadow": shadow,
                        "legacy_proposals": proposals,
                    }
                )
            cases.append(
                {
                    "case_id": case_id,
                    "repetition": repetition,
                    "database": str(db_path),
                    "assistant_turn_count": len(turns),
                    "shadow_turn_count": len(entries),
                    "turns": entries,
                }
            )
        finally:
            db.close()

    return {
        "run_dir": str(run_dir),
        "database_count": len(databases),
        "assistant_turn_count": total_assistant_turns,
        "shadow_turn_count": shadow_turns,
        "missing_shadow_turn_count": total_assistant_turns - shadow_turns,
        "cases": cases,
    }


def _atom_line(atom: dict[str, Any]) -> str:
    return json.dumps(atom, ensure_ascii=False, sort_keys=True)


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# TE2 semantic shadow comparison",
        "",
        f"Run: `{report['run_dir']}`",
        "",
        f"Isolated databases: **{report['database_count']}**  ",
        f"Assistant turns: **{report['assistant_turn_count']}**  ",
        f"Turns with TE2 shadow: **{report['shadow_turn_count']}**  ",
        f"Turns without TE2 shadow: **{report['missing_shadow_turn_count']}**",
        "",
        "This report intentionally does not decide semantic equivalence by string matching. "
        "It places the TE2 observation graph beside the legacy persistence proposals for review.",
        "",
    ]
    for case in report["cases"]:
        lines.extend(
            [
                f"## {case['case_id']} / {case['repetition']}",
                "",
                f"Shadow turns: {case['shadow_turn_count']}/{case['assistant_turn_count']}",
                "",
            ]
        )
        for turn in case["turns"]:
            shadow = turn["te2_shadow"]
            residual = shadow.get("residual") or {}
            lines.extend(
                [
                    f"### Assistant turn `{turn['assistant_turn_id']}`",
                    "",
                    f"Structured receipts excluded from residual extraction: **{shadow.get('receipt_count', 0)}**",
                    "",
                    "TE2 residual:",
                ]
            )
            for section in ("entities", "fluents", "relations"):
                atoms = residual.get(section) or []
                lines.append(f"- **{section}** ({len(atoms)})")
                for atom in atoms:
                    lines.append(f"  - `{_atom_line(atom)}`")
            lines.extend(["", "Legacy Scribe proposals:"])
            if not turn["legacy_proposals"]:
                lines.append("- none")
            else:
                for proposal in turn["legacy_proposals"]:
                    lines.append(
                        "- `"
                        + json.dumps(proposal, ensure_ascii=False, sort_keys=True)
                        + "`"
                    )
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_report(run_dir: Path) -> tuple[Path, Path, dict[str, Any]]:
    report = collect_run(run_dir)
    json_path = run_dir / "te2-shadow-report.json"
    markdown_path = run_dir / "te2-shadow-report.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, markdown_path, report


def main() -> int:
    args = _parse_args()
    try:
        run_dir = _resolve_run_dir(args.run)
        json_path, markdown_path, report = write_report(run_dir)
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        print(f"[TE2 shadow report] ERROR: {exc}")
        return 2

    print(
        "[TE2 shadow report] "
        f"{report['shadow_turn_count']}/{report['assistant_turn_count']} assistant turns captured"
    )
    print(f"JSON: {json_path}")
    print(f"Markdown: {markdown_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
