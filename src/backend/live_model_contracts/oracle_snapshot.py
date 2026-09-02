from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _loads(value: object, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _rows(db: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    cursor = db.execute(sql, params)
    columns = [column[0] for column in cursor.description or []]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def _one(db: sqlite3.Connection, sql: str, params: tuple = ()) -> dict | None:
    rows = _rows(db, sql, params)
    return rows[0] if rows else None


def _table_exists(db: sqlite3.Connection, name: str) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone() is not None


@dataclass(frozen=True)
class TruthSnapshot:
    campaign_id: str
    data: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return self.data

    @property
    def assistant_surface(self) -> str:
        turns = [
            row
            for row in self.data["turns"]
            if row["role"] == "assistant" and row["status"] == "active"
        ]
        return turns[-1]["content"] if turns else ""

    def entity_rows(
        self,
        *,
        entity_type: str | None = None,
        name: str | None = None,
    ) -> list[dict]:
        rows = list(self.data["entities"])
        if entity_type is not None:
            rows = [row for row in rows if row["type"] == entity_type]
        if name is not None:
            key = name.casefold()
            rows = [row for row in rows if row["name"].casefold() == key]
        return rows

    def entity(self, name: str, *, entity_type: str | None = None) -> dict | None:
        rows = self.entity_rows(entity_type=entity_type, name=name)
        return rows[0] if rows else None

    def active_scene(self) -> dict | None:
        return next(
            (row for row in self.data["scenes"] if row["status"] == "active"),
            None,
        )

    def current_facts(self) -> list[dict]:
        return [row for row in self.data["facts"] if row["current"]]

    def current_beliefs(self, character_name: str | None = None) -> list[dict]:
        rows = [row for row in self.data["beliefs"] if row["current"]]
        if character_name is not None:
            key = character_name.casefold()
            rows = [
                row
                for row in rows
                if str(row.get("character") or "").casefold() == key
            ]
        return rows

    def current_relationships(self) -> list[dict]:
        return [row for row in self.data["relationships"] if row["current"]]

    def active_theses(self) -> list[dict]:
        return [row for row in self.data["theses"] if row["status"] == "active"]


def capture(db_path: Path, campaign_id: str) -> TruthSnapshot:
    """Read the actual SQLite truth store; no model is used to judge another model."""
    db = sqlite3.connect(db_path)
    try:
        campaign = _one(
            db,
            "SELECT id, name, current_scene_id, player_character_id FROM campaigns WHERE id=?",
            (campaign_id,),
        )
        if campaign is None:
            raise RuntimeError(f"campaign {campaign_id} is absent from contract database")

        base = _rows(
            db,
            """SELECT id, entity_type, canonical_name, aliases, description, status,
                      provenance, version, custom_fields
                 FROM entities WHERE campaign_id=? ORDER BY entity_type, canonical_name, id""",
            (campaign_id,),
        )
        names = {str(row["id"]): str(row["canonical_name"]) for row in base}

        characters = {
            row["entity_id"]: row
            for row in _rows(
                db,
                """SELECT entity_id, appearance, personality, voice, current_location_id,
                          emotional_state FROM characters""",
            )
        }
        locations = {
            row["entity_id"]: row
            for row in _rows(
                db,
                """SELECT entity_id, geography, atmosphere, access_rules, parent_location_id,
                          climate, notable_features, danger_level FROM locations""",
            )
        }
        items = {
            row["entity_id"]: row
            for row in _rows(
                db,
                "SELECT entity_id, current_owner_id, current_location_id FROM items",
            )
        }
        entities: list[dict] = []
        for row in base:
            detail: dict[str, Any] = {}
            if row["entity_type"] == "character":
                source = characters.get(row["id"], {})
                detail = {
                    "appearance": source.get("appearance"),
                    "personality": source.get("personality"),
                    "voice": source.get("voice"),
                    "location": names.get(str(source.get("current_location_id") or "")),
                    "emotional_state": source.get("emotional_state"),
                }
            elif row["entity_type"] == "location":
                source = locations.get(row["id"], {})
                detail = {
                    "geography": source.get("geography"),
                    "atmosphere": source.get("atmosphere"),
                    "access_rules": source.get("access_rules"),
                    "parent": names.get(str(source.get("parent_location_id") or "")),
                    "climate": source.get("climate"),
                    "notable_features": source.get("notable_features"),
                    "danger_level": source.get("danger_level"),
                }
            elif row["entity_type"] == "item":
                source = items.get(row["id"], {})
                detail = {
                    "owner": names.get(str(source.get("current_owner_id") or "")),
                    "location": names.get(str(source.get("current_location_id") or "")),
                }
            entities.append(
                {
                    "id": row["id"],
                    "type": row["entity_type"],
                    "name": row["canonical_name"],
                    "aliases": _loads(row["aliases"], []),
                    "description": row["description"],
                    "status": row["status"],
                    "provenance": row["provenance"],
                    "version": row["version"],
                    "custom_fields": _loads(row["custom_fields"], {}),
                    **detail,
                }
            )

        scene_rows = _rows(
            db,
            """SELECT id, title, status, location_description
                 FROM scenes WHERE campaign_id=? ORDER BY created_at, id""",
            (campaign_id,),
        )
        scene_location = {}
        if _table_exists(db, "scene_location_links"):
            scene_location = {
                row["scene_id"]: names.get(str(row["location_id"]))
                for row in _rows(
                    db,
                    """SELECT sl.scene_id, sl.location_id
                         FROM scene_location_links sl
                         JOIN scenes s ON s.id=sl.scene_id
                        WHERE s.campaign_id=?""",
                    (campaign_id,),
                )
            }
        participant_rows = _rows(
            db,
            """SELECT sp.scene_id, sp.entity_id
                 FROM scene_participants sp JOIN scenes s ON s.id=sp.scene_id
                WHERE s.campaign_id=? ORDER BY sp.scene_id, sp.entity_id""",
            (campaign_id,),
        )
        participants: dict[str, list[str]] = {}
        for row in participant_rows:
            participants.setdefault(row["scene_id"], []).append(
                names.get(str(row["entity_id"]), str(row["entity_id"]))
            )
        scenes = [
            {
                "id": row["id"],
                "title": row["title"],
                "status": row["status"],
                "location": scene_location.get(row["id"]),
                "location_description": row["location_description"],
                "participants": sorted(participants.get(row["id"], [])),
            }
            for row in scene_rows
        ]
        scene_titles = {row["id"]: row["title"] for row in scene_rows}

        facts = [
            {
                "subject": row["subject"],
                "predicate": row["predicate"],
                "object": row["object_value"],
                "truth": row["truth_status"],
                "scope": row["scope"],
                "memory_kind": row.get("memory_kind"),
                "subject_entity": names.get(str(row.get("subject_entity_id") or "")),
                "current": bool(row["is_current"]),
            }
            for row in _rows(
                db,
                """SELECT subject, predicate, object_value, truth_status, scope, memory_kind,
                          subject_entity_id, is_current
                     FROM facts WHERE campaign_id=? ORDER BY created_at, id""",
                (campaign_id,),
            )
        ]
        beliefs = [
            {
                "character": names.get(str(row["character_id"])),
                "proposition": row["proposition"],
                "status": row["status"],
                "confidence": row["confidence"],
                "source_character": names.get(str(row["source_character_id"] or "")),
                "current": bool(row["is_current"]),
            }
            for row in _rows(
                db,
                """SELECT b.character_id, b.proposition, b.status, b.confidence,
                          b.source_character_id, b.is_current
                     FROM beliefs b JOIN entities e ON e.id=b.character_id
                    WHERE e.campaign_id=? ORDER BY b.created_at, b.id""",
                (campaign_id,),
            )
        ]
        relationships = [
            {
                "subject": names.get(str(row["subject_id"])),
                "object": names.get(str(row["object_id"])),
                "type": row["relation_type"],
                "description": row["description"],
                "intensity": row["intensity"],
                "current": bool(row["is_current"]),
            }
            for row in _rows(
                db,
                """SELECT subject_id, object_id, relation_type, description, intensity, is_current
                     FROM relationship_assertions
                    WHERE campaign_id=? ORDER BY created_at, id""",
                (campaign_id,),
            )
        ]
        theses = [
            {
                "scene": scene_titles.get(row["scene_id"]),
                "type": row["thesis_type"],
                "text": row["text"],
                "priority": row["priority"],
                "status": row["status"],
                "pinned": bool(row["pinned"]),
                "related": sorted(
                    names.get(str(item), str(item))
                    for item in _loads(row["related_entity_ids"], [])
                ),
            }
            for row in _rows(
                db,
                """SELECT st.scene_id, st.thesis_type, st.text, st.priority, st.status,
                          st.pinned, st.related_entity_ids
                     FROM scene_theses st JOIN scenes s ON s.id=st.scene_id
                    WHERE s.campaign_id=? ORDER BY st.created_at, st.id""",
                (campaign_id,),
            )
        ]

        event_rows = _rows(
            db,
            """SELECT id, event_type, description, location_id
                 FROM events WHERE campaign_id=? ORDER BY created_at, id""",
            (campaign_id,),
        )
        event_participants: dict[str, list[str]] = {}
        if event_rows:
            for row in _rows(
                db,
                """SELECT ep.event_id, ep.entity_id
                     FROM event_participants ep JOIN events e ON e.id=ep.event_id
                    WHERE e.campaign_id=? ORDER BY ep.event_id, ep.entity_id""",
                (campaign_id,),
            ):
                event_participants.setdefault(row["event_id"], []).append(
                    names.get(str(row["entity_id"]), str(row["entity_id"]))
                )
        events = [
            {
                "type": row["event_type"],
                "description": row["description"],
                "location": names.get(str(row["location_id"] or "")),
                "participants": sorted(event_participants.get(row["id"], [])),
            }
            for row in event_rows
        ]

        turns = [
            {
                "role": row["role"],
                "content": row["content"],
                "status": row["status"],
                "actor": names.get(str(row["acting_character_id"] or "")),
                "model": row["model_name"],
            }
            for row in _rows(
                db,
                """SELECT role, content, status, acting_character_id, model_name
                     FROM turns WHERE campaign_id=? ORDER BY created_at, id""",
                (campaign_id,),
            )
        ]

        scene_state = None
        active = next((row for row in scenes if row["status"] == "active"), None)
        if active and _table_exists(db, "scene_runtime_states"):
            scene_state = _one(
                db,
                """SELECT world_time_label, world_time_order, scene_goal, active_conflict
                     FROM scene_runtime_states WHERE scene_id=?""",
                (active["id"],),
            )

        sequences: list[dict] = []
        if _table_exists(db, "action_sequences"):
            for sequence in _rows(
                db,
                """SELECT id, status, summary, planned_steps, completed_steps, blocked_step_index
                     FROM action_sequences WHERE campaign_id=? ORDER BY created_at, id""",
                (campaign_id,),
            ):
                sequences.append(
                    {
                        "status": sequence["status"],
                        "summary": sequence["summary"],
                        "planned": sequence["planned_steps"],
                        "completed": sequence["completed_steps"],
                        "blocked_index": sequence["blocked_step_index"],
                        "steps": _rows(
                            db,
                            """SELECT step_index, action_type, status, observable_outcome,
                                      blocking_reason, item_name, item_operation
                                 FROM action_steps WHERE sequence_id=? ORDER BY step_index""",
                            (sequence["id"],),
                        ),
                    }
                )

        generations = _rows(
            db,
            "SELECT status, error FROM generation_runs WHERE campaign_id=? ORDER BY created_at, id",
            (campaign_id,),
        )
        post_turn_jobs = _rows(
            db,
            """SELECT job_type, status, attempts, error
                 FROM post_turn_jobs WHERE campaign_id=? ORDER BY created_at, id""",
            (campaign_id,),
        )

        return TruthSnapshot(
            campaign_id=campaign_id,
            data={
                "campaign": {
                    "name": campaign["name"],
                    "player": names.get(str(campaign["player_character_id"] or "")),
                    "current_scene_id": campaign["current_scene_id"],
                },
                "entities": entities,
                "scenes": scenes,
                "scene_state": scene_state,
                "facts": facts,
                "beliefs": beliefs,
                "relationships": relationships,
                "theses": theses,
                "events": events,
                "turns": turns,
                "action_sequences": sequences,
                "generations": generations,
                "post_turn_jobs": post_turn_jobs,
            },
        )
    finally:
        db.close()


def semantic_diff(before: TruthSnapshot, after: TruthSnapshot) -> dict[str, Any]:
    """Produce a readable state delta without pretending prose equality is a contract."""
    result: dict[str, Any] = {}
    for section in (
        "entities",
        "scenes",
        "facts",
        "beliefs",
        "relationships",
        "theses",
        "events",
    ):
        old = before.data[section]
        new = after.data[section]
        old_serialized = {json.dumps(row, ensure_ascii=False, sort_keys=True) for row in old}
        new_serialized = {json.dumps(row, ensure_ascii=False, sort_keys=True) for row in new}
        added = [
            row
            for row in new
            if json.dumps(row, ensure_ascii=False, sort_keys=True) not in old_serialized
        ]
        removed = [
            row
            for row in old
            if json.dumps(row, ensure_ascii=False, sort_keys=True) not in new_serialized
        ]
        if added or removed:
            result[section] = {
                "added_or_changed": added,
                "removed_or_changed": removed,
            }
    if before.data.get("scene_state") != after.data.get("scene_state"):
        result["scene_state"] = {
            "before": before.data.get("scene_state"),
            "after": after.data.get("scene_state"),
        }
    return result
