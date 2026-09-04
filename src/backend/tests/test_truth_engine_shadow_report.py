from __future__ import annotations

import json
import sqlite3

from live_model_contracts.shadow_report import collect_run, render_markdown


def _make_database(tmp_path):
    run_dir = tmp_path / "aggregate"
    db_path = run_dir / "isolated" / "semantic-case" / "run-1" / "live-contracts.db"
    db_path.parent.mkdir(parents=True)
    db = sqlite3.connect(db_path)
    db.executescript(
        """
        CREATE TABLE turns (
            id TEXT PRIMARY KEY,
            parent_turn_id TEXT,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            context_snapshot TEXT,
            status TEXT NOT NULL,
            acting_character_id TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE proposed_changes (
            id TEXT PRIMARY KEY,
            turn_id TEXT NOT NULL,
            change_type TEXT NOT NULL,
            payload TEXT,
            status TEXT NOT NULL,
            user_edit TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE post_turn_jobs (
            id TEXT PRIMARY KEY,
            assistant_turn_id TEXT NOT NULL,
            job_type TEXT NOT NULL,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL,
            error TEXT,
            created_at TEXT NOT NULL
        );
        """
    )

    rows = [
        ("u1", None, "user", "Я замечаю, что дверь заперта.", None, "active", None, "01"),
        (
            "a1",
            "u1",
            "assistant",
            "Дверь действительно заперта.",
            json.dumps(
                {
                    "te2_semantic_shadow": {
                        "receipt_count": 0,
                        "structured_receipts": [],
                        "residual": {"entities": [], "fluents": [], "relations": []},
                    }
                }
            ),
            "active",
            None,
            "02",
        ),
        ("u2", None, "user", "Осматриваюсь.", None, "active", None, "03"),
        ("a2", "u2", "assistant", "Комната тиха.", "{}", "active", None, "04"),
        ("u3", None, "user", "Отдаю ключ стражу.", None, "active", None, "05"),
        (
            "a3",
            "u3",
            "assistant",
            "Страж принимает ключ и кивает.",
            json.dumps(
                {
                    "te2_semantic_shadow": {
                        "receipt_count": 1,
                        "structured_receipts": [
                            {
                                "event_id": "receipt-1",
                                "event_type": "item_transfer",
                                "description": "Key transferred.",
                                "payload": {"operation": "give"},
                            }
                        ],
                        "residual": {
                            "entities": [
                                {
                                    "ref": "guard",
                                    "mention_text": "страж",
                                    "entity_type": "character",
                                }
                            ],
                            "fluents": [
                                {
                                    "atom_key": "stance",
                                    "subject_ref": "guard",
                                    "semantic_description": "current social stance",
                                    "value": "approving",
                                    "description": "The guard is approving.",
                                }
                            ],
                            "relations": [],
                        },
                    }
                }
            ),
            "active",
            "actor-1",
            "06",
        ),
    ]
    db.executemany(
        "INSERT INTO turns(id,parent_turn_id,role,content,context_snapshot,status,acting_character_id,created_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        rows,
    )
    db.execute(
        "INSERT INTO proposed_changes(id,turn_id,change_type,payload,status,user_edit,created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (
            "p1",
            "a1",
            "fact",
            json.dumps({"subject": "door", "predicate": "state", "object": "locked"}),
            "accepted",
            None,
            "02",
        ),
    )
    db.executemany(
        "INSERT INTO post_turn_jobs(id,assistant_turn_id,job_type,status,attempts,error,created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        [
            ("j1", "a1", "te2_semantic_shadow", "completed", 1, None, "02"),
            ("j2", "a2", "te2_semantic_shadow", "failed", 1, "model unavailable", "04"),
            ("j3", "a3", "te2_semantic_shadow", "completed", 1, None, "06"),
        ],
    )
    db.commit()
    db.close()
    return run_dir


def test_shadow_report_builds_structural_triage_without_semantic_string_matching(tmp_path):
    report = collect_run(_make_database(tmp_path))

    assert report["database_count"] == 1
    assert report["assistant_turn_count"] == 3
    assert report["shadow_turn_count"] == 2
    assert report["missing_shadow_turn_count"] == 1
    assert report["counts"]["legacy_objective_proposals"] == 1
    assert report["counts"]["fluents"] == 1
    assert report["counts"]["receipts"] == 1
    assert report["triage_counts"] == {
        "actor_scoped_residual_review": 1,
        "missing_shadow": 1,
        "receipt_plus_residual_review": 1,
        "shadow_job_failed": 1,
        "te2_empty_with_legacy_objective": 1,
        "te2_residual_without_legacy_objective": 1,
    }

    turns = report["cases"][0]["turns"]
    assert turns[0]["player_input"] == "Я замечаю, что дверь заперта."
    assert turns[1]["shadow_job"]["error"] == "model unavailable"
    assert turns[2]["counts"]["residual_atoms"] == 1
    assert "receipt_plus_residual_review" in turns[2]["triage_flags"]

    markdown = render_markdown(report)
    assert "Structural summary" in markdown
    assert "Triage queue" in markdown
    assert "Я замечаю, что дверь заперта." in markdown
    assert "model unavailable" in markdown
    assert "Key transferred." in markdown
    assert "These flags are review queues, not semantic verdicts." in markdown
