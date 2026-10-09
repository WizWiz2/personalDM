"""Replay public scene-event conditions from the existing canonical journal."""
from __future__ import annotations

import json
from uuid import UUID

from sqlalchemy import select

from app.db.tables import Event, EventParticipant, Turn
from app.db.truth_engine_table import TruthEventRecord


class PublishedWorldState:
    def __init__(self, session):
        self._session = session

    async def project(self, campaign_id: UUID, *, observer_id: UUID | None = None,
                      local_location_id: UUID | None = None) -> dict:
        query = (select(TruthEventRecord, Event)
            .join(Event, Event.id == TruthEventRecord.event_id)
            .join(Turn, Turn.id == TruthEventRecord.source_turn_id)
            .where(TruthEventRecord.campaign_id == str(campaign_id),
                   TruthEventRecord.status == "active", Turn.status == "active",
                   Event.event_type.in_(("world_scene_development", "world_condition_update")))
            .order_by(TruthEventRecord.sequence))
        rows = (await self._session.execute(query)).all()
        observed = set((await self._session.execute(select(EventParticipant.event_id).where(
            EventParticipant.entity_id == str(observer_id)))).scalars()) if observer_id else set()
        states = {}
        legacy = []
        for record, event in rows:
            payload = json.loads(record.payload_json or "{}")
            updates = payload.get("state_updates") or []
            origin = {"location_id": event.location_id, "event_id": record.event_id}
            for update in updates:
                if update.get("state_id"):
                    states[update["state_id"]] = {
                        **origin, "location_id": states.get(update["state_id"], origin)["location_id"],
                        "state_id": update["state_id"],
                        "subject": update["subject"], "value": update["value"],
                    }
            if not updates and payload.get("development"):
                # Old events have no slot identities. Preserve ordered public changes as
                # continuity evidence, never private rationale or an invented migration.
                legacy.append({**origin, "change": payload["development"]})
        def visible(item):
            return (not observer_id or item["event_id"] in observed
                    or local_location_id is not None and item["location_id"] == str(local_location_id))

        # Fold before visibility filtering: an observer's older receipt cannot resurrect
        # a superseded value when the latest value is outside their available knowledge.
        return {"conditions": [item for item in states.values() if visible(item)],
                "legacy_changes": [item for item in legacy if visible(item)][-8:]}
