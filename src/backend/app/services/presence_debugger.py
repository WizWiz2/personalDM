import json
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.scene_location_table import SceneLocationLink
from app.db.tables import (
    Character,
    Entity,
    Event,
    EventParticipant,
    Scene,
    SceneParticipant,
)


def _json_dict(value: str | None) -> dict:
    if not value:
        return {}
    try:
        decoded = json.loads(value)
        return decoded if isinstance(decoded, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


class PresenceDebugger:
    """Report structured NPC identity, scene membership and event participation."""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def snapshot(self, campaign_id: UUID) -> dict:
        entities = (
            await self._session.execute(
                select(Entity).where(Entity.campaign_id == str(campaign_id))
            )
        ).scalars().all()
        entity_map = {row.id: row for row in entities}
        entity_names = {row.id: row.canonical_name for row in entities}
        character_ids = [
            row.id for row in entities if row.entity_type == "character"
        ]
        characters = []
        if character_ids:
            characters = (
                await self._session.execute(
                    select(Character).where(Character.entity_id.in_(character_ids))
                )
            ).scalars().all()
        character_map = {row.entity_id: row for row in characters}

        scenes = (
            await self._session.execute(
                select(Scene)
                .where(Scene.campaign_id == str(campaign_id))
                .order_by(Scene.created_at)
            )
        ).scalars().all()
        scene_ids = [row.id for row in scenes]
        scene_map = {row.id: row for row in scenes}

        scene_locations: dict[str, str] = {}
        participants = []
        if scene_ids:
            links = (
                await self._session.execute(
                    select(SceneLocationLink).where(
                        SceneLocationLink.scene_id.in_(scene_ids)
                    )
                )
            ).scalars().all()
            scene_locations = {row.scene_id: row.location_id for row in links}
            participants = (
                await self._session.execute(
                    select(SceneParticipant).where(
                        SceneParticipant.scene_id.in_(scene_ids)
                    )
                )
            ).scalars().all()

        presence_issues: list[str] = []
        memberships: dict[str, list[str]] = {}
        for row in participants:
            memberships.setdefault(row.entity_id, []).append(row.scene_id)
            scene = scene_map.get(row.scene_id)
            entity = entity_map.get(row.entity_id)
            character = character_map.get(row.entity_id)
            if not scene:
                presence_issues.append(
                    f"participant {row.entity_id} references missing scene {row.scene_id}"
                )
                continue
            if not entity or entity.entity_type != "character":
                presence_issues.append(
                    f"scene {scene.title} contains missing or non-character participant {row.entity_id}"
                )
                continue
            if not character:
                presence_issues.append(
                    f"scene participant {entity.canonical_name} has no character-state row"
                )
                continue
            if scene.status != "active":
                continue
            scene_location = scene_locations.get(scene.id)
            if scene_location and character.current_location_id != scene_location:
                presence_issues.append(
                    f"{entity.canonical_name} participates in «{scene.title}» at "
                    f"{entity_names.get(scene_location, scene_location)}, but current_location_id "
                    f"is {entity_names.get(character.current_location_id, character.current_location_id)}"
                )

        for entity_id, member_scene_ids in memberships.items():
            active = [
                scene_id
                for scene_id in member_scene_ids
                if scene_map.get(scene_id) and scene_map[scene_id].status == "active"
            ]
            if len(active) > 1:
                presence_issues.append(
                    f"{entity_names.get(entity_id, entity_id)} appears in {len(active)} active scenes"
                )

        events = (
            await self._session.execute(
                select(Event)
                .where(Event.campaign_id == str(campaign_id))
                .order_by(Event.created_at)
            )
        ).scalars().all()
        event_ids = [row.id for row in events]
        event_participants: dict[str, list[str]] = {event_id: [] for event_id in event_ids}
        if event_ids:
            rows = (
                await self._session.execute(
                    select(EventParticipant).where(
                        EventParticipant.event_id.in_(event_ids)
                    )
                )
            ).scalars().all()
            for row in rows:
                event_participants.setdefault(row.event_id, []).append(row.entity_id)

        empty_events = [
            {
                "id": row.id,
                "event_type": row.event_type,
                "description": row.description,
                "location_id": row.location_id,
                "location_name": entity_names.get(row.location_id),
            }
            for row in events
            if not event_participants.get(row.id)
        ]

        auto_registered = []
        for entity in entities:
            if entity.entity_type != "character" or entity.provenance != "narrator_extracted":
                continue
            character = character_map.get(entity.id)
            fields = _json_dict(entity.custom_fields)
            scene_ids_for_entity = memberships.get(entity.id, [])
            auto_registered.append(
                {
                    "id": entity.id,
                    "name": entity.canonical_name,
                    "aliases": json.loads(entity.aliases or "[]"),
                    "description": entity.description,
                    "provenance": entity.provenance,
                    "current_location_id": (
                        character.current_location_id if character else None
                    ),
                    "current_location_name": entity_names.get(
                        character.current_location_id if character else None
                    ),
                    "scene_ids": scene_ids_for_entity,
                    "scene_titles": [
                        scene_map[scene_id].title
                        for scene_id in scene_ids_for_entity
                        if scene_id in scene_map
                    ],
                    "source_turn_id": fields.get("source_turn_id"),
                    "first_seen_scene_id": fields.get("first_seen_scene_id"),
                    "role": fields.get("role"),
                    "importance": fields.get("importance"),
                    "temporary_name": bool(fields.get("temporary_name")),
                }
            )

        return {
            "presence_state_issues": presence_issues,
            "auto_registered_npcs": auto_registered,
            "event_participants": [
                {
                    "event_id": event.id,
                    "event_type": event.event_type,
                    "participant_ids": event_participants.get(event.id, []),
                    "participant_names": [
                        entity_names.get(entity_id, entity_id)
                        for entity_id in event_participants.get(event.id, [])
                    ],
                }
                for event in events
            ],
            "events_without_participants": empty_events,
            "health": {
                "presence_state_errors": len(presence_issues),
                "auto_registered_npcs": len(auto_registered),
                "events_without_participants": len(empty_events),
            },
        }
