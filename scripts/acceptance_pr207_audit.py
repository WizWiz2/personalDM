#!/usr/bin/env python3
"""Read-only post-turn and frame acceptance probe for a PersonalDM SQLite library.

Example (Windows):
  python scripts/acceptance_pr207_audit.py "%APPDATA%\\PersonalDM\\library\\campaign.db" --campaign YOUR-UUID

Take a backup before running a live playtest. This probe itself never writes.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


def stamp(value: str | None) -> datetime | None:
    if not value:
        return None
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result


def audit(db: Path, campaign: str | None, stale_after: int) -> dict:
    url = db.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(url, uri=True, timeout=5) as con:
        con.row_factory = sqlite3.Row
        campaigns = con.execute(
            "SELECT id,name,current_scene_id,player_character_id FROM campaigns"
            + (" WHERE id=?" if campaign else ""),
            (campaign,) if campaign else (),
        ).fetchall()
        report = {"database": str(db), "campaigns": [], "stale_after_seconds": stale_after}
        for c in campaigns:
            cid = c["id"]
            jobs = con.execute(
                "SELECT id,job_type,status,attempts,locked_at,updated_at,error "
                "FROM post_turn_jobs WHERE campaign_id=?", (cid,)
            ).fetchall()
            counts = Counter((j["job_type"], j["status"]) for j in jobs)
            statuses = {
                name: dict(Counter(j["status"] for j in jobs if j["job_type"] == name))
                for name in sorted({j["job_type"] for j in jobs})
            }
            now = datetime.now(timezone.utc)
            stale = []
            failed = []
            for j in jobs:
                if j["status"] == "failed":
                    failed.append({"id": j["id"], "job": j["job_type"],
                                   "attempts": j["attempts"], "error": (j["error"] or "")[:160]})
                if j["status"] == "running":
                    lease = stamp(j["locked_at"] or j["updated_at"])
                    age = int((now - lease).total_seconds()) if lease else None
                    if age is None or age > stale_after:
                        stale.append({"id": j["id"], "job": j["job_type"],
                                      "age_s": age, "locked_at_null": j["locked_at"] is None})
            scene = con.execute(
                "SELECT sl.location_id AS scene_location_id, ch.current_location_id "
                "AS player_location_id, sp.entity_id AS player_present "
                "FROM campaigns c "
                "LEFT JOIN scene_location_links sl ON sl.scene_id=c.current_scene_id "
                "LEFT JOIN characters ch ON ch.entity_id=c.player_character_id "
                "LEFT JOIN scene_participants sp ON sp.scene_id=c.current_scene_id "
                "AND sp.entity_id=c.player_character_id "
                "WHERE c.id=?", (cid,)
            ).fetchone()
            frame = {
                "active_scene": c["current_scene_id"],
                "scene_location": scene["scene_location_id"] if scene else None,
                "player_location": scene["player_location_id"] if scene else None,
                "player_present": bool(scene and scene["player_present"]),
            }
            frame["consistent"] = (
                frame["scene_location"] == frame["player_location"]
                and (not c["player_character_id"] or frame["player_present"])
            )
            published = con.execute(
                "SELECT count(*) FROM turns WHERE campaign_id=? AND role='assistant' AND status='active'",
                (cid,),
            ).fetchone()[0]
            problems = sorted({
                row["assistant_turn_id"] for row in con.execute(
                    "SELECT assistant_turn_id FROM post_turn_jobs "
                    "WHERE campaign_id=? AND job_type='memory_scribe' "
                    "AND status IN ('failed','running')", (cid,)
                )
            })
            report["campaigns"].append({
                "id": cid, "name": c["name"], "published_active": published,
                "jobs": statuses, "failed": failed, "stale": stale,
                "memory_jobs_not_complete": len(problems),
                "memory_jobs_not_complete_pct_of_published": (
                    round(100 * len(problems) / published, 2) if published else None
                ),
                "frame": frame,
                "p0_detected": bool(failed or stale or not frame["consistent"]),
            })
        return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db", help="Path to campaign.db; may contain %APPDATA%")
    parser.add_argument("--campaign", help="Limit to one campaign UUID")
    parser.add_argument("--timeout", type=int, default=600, help="Post-turn timeout seconds")
    args = parser.parse_args()
    db = Path(os.path.expandvars(os.path.expanduser(args.db)))
    if not db.is_file():
        parser.error(f"Database not found: {db}")
    report = audit(db, args.campaign, max(60, args.timeout + 60))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if any(c["p0_detected"] for c in report["campaigns"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
