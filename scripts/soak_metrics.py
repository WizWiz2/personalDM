"""Read-only, reproducible soak metrics. Run against a consistent SQLite backup."""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path


def metrics(path: Path, campaign_id: str | None = None) -> dict:
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        campaigns = [row[0] for row in conn.execute("SELECT id FROM campaigns")]
        if campaign_id is None:
            if len(campaigns) != 1:
                raise ValueError("Specify --campaign when the database does not contain exactly one campaign")
            campaign_id = campaigns[0]

        def rows(sql):
            return [dict(row) for row in conn.execute(sql, (campaign_id,))]

        turns = rows("SELECT id,content,created_at FROM turns WHERE campaign_id=? AND role='assistant' "
                     "AND status='active' ORDER BY created_at,id")
        runs = rows("SELECT id,status,error,created_at FROM generation_runs WHERE campaign_id=? ORDER BY created_at,id")
        usage = rows("SELECT * FROM llm_usage_events WHERE campaign_id=? ORDER BY created_at,id")
        facts = rows("SELECT f.*,p.subject_entity_id FROM facts f LEFT JOIN fact_memory_profiles p ON p.fact_id=f.id "
                     "WHERE f.campaign_id=? AND f.is_current=1")
        beliefs = rows("SELECT b.* FROM beliefs b JOIN entities e ON e.id=b.character_id WHERE e.campaign_id=?")
        scenes = rows("SELECT s.id,l.location_id FROM scenes s LEFT JOIN scene_location_links l ON l.scene_id=s.id "
                      "WHERE s.campaign_id=?")
        characters = rows("SELECT id,canonical_name FROM entities WHERE campaign_id=? AND entity_type='character'")
        validations = rows("SELECT status FROM narration_validation_runs WHERE campaign_id=?")
        blocks = []
        for start in range(0, len(turns), 50):
            group = turns[start:start + 50]
            begin = turns[start - 1]["created_at"] if start else ""
            end = group[-1]["created_at"]
            group_runs = [run for run in runs if begin < run["created_at"] <= end]
            # Include calls finishing after the assistant timestamp through run identity.
            ids = {run["id"] for run in group_runs}
            events = [event for event in usage if event["generation_run_id"] in ids]
            blocks.append({"gm_from": start + 1, "gm_to": start + len(group), "runs": len(group_runs),
                "input_per_run": round(sum(event["input_tokens"] or 0 for event in events) / max(1,len(group_runs))),
                "usd_per_run": round(sum(event["estimated_cost_usd"] or 0 for event in events) / max(1,len(group_runs)),6),
                "avg_gm_chars": round(sum(len(turn["content"]) for turn in group) / len(group))})
        return {"database": str(path), "campaign_id": campaign_id, "gm": len(turns),
            "runs": dict(Counter(run["status"] for run in runs)),
            "input_tokens": sum(event["input_tokens"] or 0 for event in usage),
            "output_tokens": sum(event["output_tokens"] or 0 for event in usage),
            "estimated_usd": round(sum(event["estimated_cost_usd"] or 0 for event in usage),6),
            "current_facts": len(facts), "unbound_current_facts": sum(not fact["subject_entity_id"] for fact in facts),
            "physical_position_facts": sum(fact["predicate"] in {"holder","location"} for fact in facts),
            "beliefs": len(beliefs), "current_beliefs": sum(bool(belief["is_current"]) for belief in beliefs),
            "superseded_beliefs": sum(bool(belief["superseded_by"]) for belief in beliefs),
            "scenes": len(scenes), "scenes_by_location": dict(Counter(scene["location_id"] for scene in scenes)),
            "characters": characters, "validation_statuses": dict(Counter(v["status"] for v in validations)),
            "blocks": blocks,
            "caveat": "Scene count includes intentional visits. Character count alone cannot prove duplicate identity."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--campaign")
    args = parser.parse_args()
    print(json.dumps(metrics(args.database,args.campaign),ensure_ascii=False,indent=2))
