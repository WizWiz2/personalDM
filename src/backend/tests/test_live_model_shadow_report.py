from __future__ import annotations

import json
import sqlite3

from live_model_contracts.shadow_report import collect_run, render_markdown, write_report


def test_shadow_report_pairs_te2_residuals_with_legacy_proposals(tmp_path):
    run_dir = tmp_path / "live-run"
    db_path = run_dir / "isolated" / "semantic-case" / "run-1" / "live-contracts.db"
    db_path.parent.mkdir(parents=True)

    shadow = {
        "version": 1,
        "mode": "read_only",
        "source_user_turn_id": "user-1",
        "receipt_count": 1,
        "structured_receipts": [{"event_type": "item_transfer"}],
        "residual": {
            "entities": [
                {"ref": "keeper", "mention_text": "keeper", "entity_type": "character"}
            ],
            "fluents": [
                {
                    "atom_key": "mood-state",
                    "subject_ref": "keeper",
                    "semantic_description": "current outward stance",
                    "value": "friendly",
                    "description": "The keeper is friendly.",
                }
            ],
            "relations": [],
        },
        "counts": {"entities": 1, "fluents": 1, "relations": 0},
    }

    with sqlite3.connect(db_path) as db:
        db.executescript(
            """
            CREATE TABLE turns (
                id TEXT PRIMARY KEY,
                parent_turn_id TEXT,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                context_snapshot TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE proposed_changes (
                id TEXT PRIMARY KEY,
                turn_id TEXT NOT NULL,
                change_type TEXT NOT NULL,
                payload TEXT NOT NULL,
                status TEXT NOT NULL,
                user_edit TEXT,
                created_at TEXT NOT NULL
            );
            """
        )
        db.execute(
            "INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "assistant-1",
                "user-1",
                "assistant",
                "The keeper smiles.",
                json.dumps({"te2_semantic_shadow": shadow}),
                "active",
                "2026-09-04T00:00:00",
            ),
        )
        db.execute(
            "INSERT INTO proposed_changes VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "proposal-1",
                "assistant-1",
                "fact",
                json.dumps(
                    {
                        "subject": "keeper",
                        "predicate": "stance",
                        "object_value": "friendly",
                    }
                ),
                "accepted",
                None,
                "2026-09-04T00:00:01",
            ),
        )
        db.commit()

    report = collect_run(run_dir)

    assert report["database_count"] == 1
    assert report["assistant_turn_count"] == 1
    assert report["shadow_turn_count"] == 1
    turn = report["cases"][0]["turns"][0]
    assert turn["te2_shadow"]["residual"]["fluents"][0]["atom_key"] == "mood-state"
    assert turn["legacy_proposals"][0]["change_type"] == "fact"
    assert turn["legacy_proposals"][0]["payload"]["object_value"] == "friendly"

    markdown = render_markdown(report)
    assert "semantic-case / run-1" in markdown
    assert "TE2 residual" in markdown
    assert "Legacy Scribe proposals" in markdown
    assert "semantic equivalence by string matching" in markdown

    json_path, markdown_path, written = write_report(run_dir)
    assert written["shadow_turn_count"] == 1
    assert json_path.exists()
    assert markdown_path.exists()


def test_shadow_report_survives_partial_failed_contract_databases(tmp_path):
    run_dir = tmp_path / "live-run"

    empty_db = run_dir / "isolated" / "failed-before-schema" / "run-1" / "live-contracts.db"
    empty_db.parent.mkdir(parents=True)
    sqlite3.connect(empty_db).close()

    turns_only_db = run_dir / "isolated" / "failed-before-scribe" / "run-1" / "live-contracts.db"
    turns_only_db.parent.mkdir(parents=True)
    with sqlite3.connect(turns_only_db) as db:
        db.executescript(
            """
            CREATE TABLE turns (
                id TEXT PRIMARY KEY,
                parent_turn_id TEXT,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                context_snapshot TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        db.execute(
            "INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "assistant-partial",
                "user-partial",
                "assistant",
                "Partial turn.",
                json.dumps(
                    {
                        "te2_semantic_shadow": {
                            "version": 1,
                            "mode": "read_only",
                            "receipt_count": 0,
                            "residual": {"entities": [], "fluents": [], "relations": []},
                        }
                    }
                ),
                "active",
                "2026-09-04T00:00:00",
            ),
        )
        db.commit()

    report = collect_run(run_dir)

    assert report["database_count"] == 2
    assert report["assistant_turn_count"] == 1
    assert report["shadow_turn_count"] == 1
    cases = {case["case_id"]: case for case in report["cases"]}
    assert cases["failed-before-schema"]["assistant_turn_count"] == 0
    assert cases["failed-before-scribe"]["turns"][0]["legacy_proposals"] == []
