from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

SHADOW_KEY = "te2_semantic_shadow"
OBJECTIVE_LEGACY_TYPES = frozenset({"fact", "relationship"})
TERMINAL_JOB_STATUSES = frozenset({"completed", "failed", "skipped", "cancelled"})
DISPOSITIONS = (
    "objective",
    "epistemic",
    "transient",
    "receipt_owned",
    "presentation",
    "unsupported",
)
SANITIZATION_FIELDS = (
    "duplicate_entity_refs_dropped",
    "dangling_fluents_dropped",
    "dangling_relations_dropped",
    "duplicate_fluents_dropped",
    "duplicate_relations_dropped",
)


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Collect TE2 semantic shadow candidates, disposition-gate results, writer-equivalent "
            "objective residuals and legacy Scribe proposals from isolated live-model databases."
        )
    )
    parser.add_argument(
        "--run",
        type=Path,
        default=None,
        help=(
            "Live-contract aggregate run directory. Defaults to "
            "data/live-model-contracts/latest/run-path.txt."
        ),
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
        return json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _table_exists(db: sqlite3.Connection, name: str) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone() is not None


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(db, table):
        return set()
    return {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})").fetchall()}


def _case_identity(db_path: Path, run_dir: Path) -> tuple[str, str]:
    try:
        relative = db_path.relative_to(run_dir)
    except ValueError:
        return "unknown", "unknown"
    parts = relative.parts
    if len(parts) >= 4 and parts[0] == "isolated":
        return parts[1], parts[2]
    return "unknown", "unknown"


def _shadow_job(db: sqlite3.Connection, assistant_turn_id: str) -> dict[str, Any] | None:
    if not _table_exists(db, "post_turn_jobs"):
        return None
    columns = _columns(db, "post_turn_jobs")
    if not {"assistant_turn_id", "job_type", "status"}.issubset(columns):
        return None
    selected = ["status"]
    for optional in ("attempts", "error"):
        if optional in columns:
            selected.append(optional)
    order = " ORDER BY created_at DESC, id DESC" if {"created_at", "id"}.issubset(columns) else ""
    row = db.execute(
        f"SELECT {', '.join(selected)} FROM post_turn_jobs "
        "WHERE assistant_turn_id=? AND job_type='te2_semantic_shadow'"
        + order
        + " LIMIT 1",
        (assistant_turn_id,),
    ).fetchone()
    if row is None:
        return None
    return {key: row[key] for key in selected}


def _user_content(db: sqlite3.Connection, parent_turn_id: str | None) -> str | None:
    if not parent_turn_id:
        return None
    row = db.execute(
        "SELECT content FROM turns WHERE id=? AND role='user' LIMIT 1",
        (parent_turn_id,),
    ).fetchone()
    return str(row["content"]) if row is not None else None


def _legacy_proposals(
    db: sqlite3.Connection,
    assistant_turn_id: str,
    *,
    has_proposals: bool,
) -> list[dict[str, Any]]:
    if not has_proposals:
        return []
    rows = db.execute(
        """SELECT change_type, payload, status, user_edit
             FROM proposed_changes
            WHERE turn_id=? ORDER BY created_at, id""",
        (assistant_turn_id,),
    ).fetchall()
    return [
        {
            "change_type": proposal["change_type"],
            "status": proposal["status"],
            "payload": _loads(proposal["payload"], proposal["payload"]),
            "user_edit": _loads(proposal["user_edit"], proposal["user_edit"]),
        }
        for proposal in rows
    ]


def _graph_counts(graph: dict[str, Any] | None) -> dict[str, int]:
    graph = graph or {}
    return {
        "entities": len(graph.get("entities") or []),
        "fluents": len(graph.get("fluents") or []),
        "relations": len(graph.get("relations") or []),
    }


def _shadow_counts(
    shadow: dict[str, Any] | None,
    legacy_proposals: list[dict[str, Any]],
) -> dict[str, int]:
    shadow = shadow or {}
    raw = _graph_counts(shadow.get("residual"))
    objective_graph = shadow.get("objective_residual")
    # Backward compatibility for pre-gate shadow snapshots: until objective_residual exists, the old
    # residual is the only available approximation. New runs always persist writer-equivalent input.
    objective = _graph_counts(objective_graph if isinstance(objective_graph, dict) else shadow.get("residual"))
    counts = {
        "entities": raw["entities"],
        "fluents": raw["fluents"],
        "relations": raw["relations"],
        "residual_atoms": raw["fluents"] + raw["relations"],
        "objective_entities": objective["entities"],
        "objective_fluents": objective["fluents"],
        "objective_relations": objective["relations"],
        "objective_atoms": objective["fluents"] + objective["relations"],
        "legacy_objective_proposals": sum(
            str(proposal.get("change_type") or "").casefold() in OBJECTIVE_LEGACY_TYPES
            for proposal in legacy_proposals
        ),
        "receipts": int(shadow.get("receipt_count") or 0),
    }
    dispositions = Counter(
        str(item.get("disposition") or "")
        for item in (shadow.get("dispositions") or [])
        if isinstance(item, dict)
    )
    for disposition in DISPOSITIONS:
        counts[f"disposition_{disposition}"] = dispositions.get(disposition, 0)

    sanitation = shadow.get("sanitization") or {}
    sanitation_total = 0
    for field in SANITIZATION_FIELDS:
        value = int(sanitation.get(field) or 0) if isinstance(sanitation, dict) else 0
        counts[f"sanitization_{field}"] = value
        sanitation_total += value
    counts["sanitization_dropped_atoms"] = sanitation_total

    objective_graph = objective_graph if isinstance(objective_graph, dict) else {}
    counts["objective_relation_retractions"] = sum(
        atom.get("present") is False
        for atom in (objective_graph.get("relations") or [])
        if isinstance(atom, dict)
    )
    return counts


def _triage_flags(
    *,
    shadow: dict[str, Any] | None,
    shadow_job: dict[str, Any] | None,
    legacy_proposals: list[dict[str, Any]],
    actor_scoped: bool,
) -> tuple[list[str], dict[str, int]]:
    counts = _shadow_counts(shadow, legacy_proposals)
    flags: list[str] = []
    if shadow is None:
        flags.append("missing_shadow")
    if shadow_job and shadow_job.get("status") == "failed":
        flags.append("shadow_job_failed")
    if shadow_job and shadow_job.get("status") not in TERMINAL_JOB_STATUSES:
        flags.append("shadow_job_nonterminal")
    if shadow is not None and counts["objective_atoms"] == 0 and counts["legacy_objective_proposals"]:
        flags.append("te2_empty_with_legacy_objective")
    if shadow is not None and counts["objective_atoms"] and not counts["legacy_objective_proposals"]:
        flags.append("te2_residual_without_legacy_objective")
    if counts["receipts"] and counts["residual_atoms"]:
        flags.append("receipt_plus_residual_review")
    if counts["receipts"] and counts["objective_atoms"]:
        flags.append("receipt_plus_objective_review")
    if actor_scoped and counts["residual_atoms"]:
        flags.append("actor_scoped_residual_review")
    if actor_scoped and counts["objective_atoms"]:
        flags.append("actor_scoped_objective_blocker")
    if counts["sanitization_dropped_atoms"]:
        flags.append("sanitization_repair_review")
    return flags, counts


def collect_run(run_dir: Path) -> dict[str, Any]:
    databases = sorted((run_dir / "isolated").glob("*/run-*/live-contracts.db"))
    cases: list[dict[str, Any]] = []
    total_assistant_turns = 0
    shadow_turns = 0
    aggregate_counts: Counter[str] = Counter()
    triage_counts: Counter[str] = Counter()

    for db_path in databases:
        case_id, repetition = _case_identity(db_path, run_dir)
        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row
        try:
            if not _table_exists(db, "turns"):
                cases.append(
                    {
                        "case_id": case_id,
                        "repetition": repetition,
                        "database": str(db_path),
                        "assistant_turn_count": 0,
                        "shadow_turn_count": 0,
                        "turns": [],
                    }
                )
                continue

            turn_columns = _columns(db, "turns")
            acting_expr = "acting_character_id" if "acting_character_id" in turn_columns else "NULL"
            turns = db.execute(
                f"""SELECT id, parent_turn_id, content, context_snapshot, status,
                           {acting_expr} AS acting_character_id
                      FROM turns
                     WHERE role='assistant'
                     ORDER BY created_at, id"""
            ).fetchall()
            has_proposals = _table_exists(db, "proposed_changes")
            entries: list[dict[str, Any]] = []
            case_shadow_turns = 0
            for turn in turns:
                total_assistant_turns += 1
                snapshot = _loads(turn["context_snapshot"], {})
                shadow = snapshot.get(SHADOW_KEY) if isinstance(snapshot, dict) else None
                if not isinstance(shadow, dict):
                    shadow = None
                else:
                    shadow_turns += 1
                    case_shadow_turns += 1

                proposals = _legacy_proposals(
                    db,
                    str(turn["id"]),
                    has_proposals=has_proposals,
                )
                job = _shadow_job(db, str(turn["id"]))
                flags, counts = _triage_flags(
                    shadow=shadow,
                    shadow_job=job,
                    legacy_proposals=proposals,
                    actor_scoped=bool(turn["acting_character_id"]),
                )
                aggregate_counts.update(counts)
                triage_counts.update(flags)
                entries.append(
                    {
                        "assistant_turn_id": turn["id"],
                        "parent_user_turn_id": turn["parent_turn_id"],
                        "turn_status": turn["status"],
                        "acting_character_id": turn["acting_character_id"],
                        "player_input": _user_content(db, turn["parent_turn_id"]),
                        "assistant_content": turn["content"],
                        "shadow_job": job,
                        "te2_shadow": shadow,
                        "legacy_proposals": proposals,
                        "counts": counts,
                        "triage_flags": flags,
                    }
                )
            cases.append(
                {
                    "case_id": case_id,
                    "repetition": repetition,
                    "database": str(db_path),
                    "assistant_turn_count": len(turns),
                    "shadow_turn_count": case_shadow_turns,
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
        "counts": dict(sorted(aggregate_counts.items())),
        "triage_counts": dict(sorted(triage_counts.items())),
        "cases": cases,
    }


def _atom_line(atom: dict[str, Any]) -> str:
    return json.dumps(atom, ensure_ascii=False, sort_keys=True)


def _code_block(lines: list[str], title: str, value: str | None) -> None:
    lines.extend([title, "", "```text", value or "", "```", ""])


def _render_graph(lines: list[str], title: str, graph: dict[str, Any] | None) -> None:
    graph = graph or {}
    lines.append(title)
    for section in ("entities", "fluents", "relations"):
        atoms = graph.get(section) or []
        lines.append(f"- **{section}** ({len(atoms)})")
        for atom in atoms:
            lines.append(f"  - `{_atom_line(atom)}`")
    lines.append("")


def render_markdown(report: dict[str, Any]) -> str:
    counts = report.get("counts") or {}
    triage = report.get("triage_counts") or {}
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
        "## Structural summary",
        "",
        f"Raw residual fluent atoms: **{counts.get('fluents', 0)}**  ",
        f"Raw residual relation atoms: **{counts.get('relations', 0)}**  ",
        f"Writer-equivalent objective fluent atoms: **{counts.get('objective_fluents', 0)}**  ",
        f"Writer-equivalent objective relation atoms: **{counts.get('objective_relations', 0)}**  ",
        f"Objective relation retractions (`present=false`): **{counts.get('objective_relation_retractions', 0)}**  ",
        f"Legacy objective FACT/RELATIONSHIP proposals: **{counts.get('legacy_objective_proposals', 0)}**  ",
        f"Structured receipts supplied to shadow: **{counts.get('receipts', 0)}**  ",
        f"Atoms repaired/dropped by deterministic sanitation: **{counts.get('sanitization_dropped_atoms', 0)}**",
        "",
        "### Disposition totals",
        "",
    ]
    for disposition in DISPOSITIONS:
        lines.append(f"- **{disposition}**: {counts.get(f'disposition_{disposition}', 0)}")
    lines.extend(["", "## Triage queue", ""])
    if not triage:
        lines.append("- no structural review flags")
    else:
        for flag, count in sorted(triage.items()):
            lines.append(f"- **{flag}**: {count}")
    lines.extend(
        [
            "",
            (
                "These flags are review queues, not semantic verdicts. The report does not decide "
                "equivalence through lexical matching. `objective_residual` is the exact semantic "
                "graph that writer mode would receive after the bounded disposition gate."
            ),
            "",
        ]
    )

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
            shadow = turn.get("te2_shadow")
            lines.extend(
                [
                    f"### Assistant turn `{turn['assistant_turn_id']}`",
                    "",
                    "Triage: "
                    + (
                        ", ".join(f"`{flag}`" for flag in turn["triage_flags"])
                        if turn["triage_flags"]
                        else "none"
                    ),
                    "",
                    f"Shadow job: `{json.dumps(turn.get('shadow_job'), ensure_ascii=False, sort_keys=True)}`",
                    "",
                    f"Actor-scoped: **{'yes' if turn.get('acting_character_id') else 'no'}**",
                    "",
                ]
            )
            _code_block(lines, "Player input:", turn.get("player_input"))
            _code_block(lines, "Published narration:", turn.get("assistant_content"))

            if shadow is None:
                lines.extend(["TE2 shadow: **missing**", ""])
            else:
                receipts = shadow.get("structured_receipts") or []
                lines.extend(
                    [
                        f"Structured receipts supplied to extraction: **{shadow.get('receipt_count', 0)}**",
                        "",
                        "Sanitization audit:",
                        f"- `{json.dumps(shadow.get('sanitization') or {}, ensure_ascii=False, sort_keys=True)}`",
                        "",
                    ]
                )
                if receipts:
                    lines.append("Structured receipts:")
                    for receipt in receipts:
                        lines.append(f"- `{_atom_line(receipt)}`")
                    lines.append("")
                _render_graph(lines, "TE2 residual candidates:", shadow.get("residual"))
                lines.append("Disposition gate:")
                for decision in shadow.get("dispositions") or []:
                    lines.append(f"- `{_atom_line(decision)}`")
                if not shadow.get("dispositions"):
                    lines.append("- none / pre-gate snapshot")
                lines.append("")
                _render_graph(
                    lines,
                    "TE2 writer-equivalent objective residual:",
                    shadow.get("objective_residual")
                    if isinstance(shadow.get("objective_residual"), dict)
                    else shadow.get("residual"),
                )

            lines.append("Legacy Scribe proposals:")
            if not turn["legacy_proposals"]:
                lines.append("- none")
            else:
                for proposal in turn["legacy_proposals"]:
                    lines.append(
                        "- `" + json.dumps(proposal, ensure_ascii=False, sort_keys=True) + "`"
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
        print(f"TE2 shadow report failed: {exc}")
        return 1

    print(f"TE2 shadow JSON: {json_path}")
    print(f"TE2 shadow Markdown: {markdown_path}")
    print(
        "TE2 shadow summary: "
        f"{report['shadow_turn_count']}/{report['assistant_turn_count']} assistant turns captured; "
        f"objective atoms={report.get('counts', {}).get('objective_atoms', 0)}; "
        f"triage flags={sum((report.get('triage_counts') or {}).values())}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
