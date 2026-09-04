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
