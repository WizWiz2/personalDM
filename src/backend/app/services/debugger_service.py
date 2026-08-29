import json
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.generation_lifecycle_table import GenerationLifecycle
from app.db.scene_location_table import SceneLocationLink
from app.db.tables import (
    Belief,
    Campaign,
    Character,
    Entity,
    Event,
    Fact,
    GenerationRun,
    Location,
    PostTurnJob,
    ProposedChange,
    RelationshipAssertion,
    Scene,
    SceneParticipant,
    SceneThesis,
    Turn,
)


def _json(value, fallback):
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _iso(value):
    return value.isoformat() if value else None


class DebuggerService:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def snapshot(self, campaign_id: UUID, turn_limit: int = 100) -> dict:
        campaign = await self._session.get(Campaign, str(campaign_id))
        if not campaign:
            raise ValueError("Campaign not found")

        entities = (
            await self._session.execute(
                select(Entity)
                .where(Entity.campaign_id == str(campaign_id))
                .order_by(Entity.entity_type, Entity.canonical_name)
            )
        ).scalars().all()
        entity_names = {row.id: row.canonical_name for row in entities}
        entity_map = {row.id: row for row in entities}

        location_ids = [row.id for row in entities if row.entity_type == "location"]
        location_details = []
        if location_ids:
            location_details = (
                await self._session.execute(
                    select(Location).where(Location.entity_id.in_(location_ids))
                )
            ).scalars().all()
        location_parent = {
            row.entity_id: row.parent_location_id for row in location_details
        }
        location_detail_map = {row.entity_id: row for row in location_details}

        def location_path(location_id: str | None) -> list[str]:
            if not location_id:
                return []
            names: list[str] = []
            visited: set[str] = set()
            current = location_id
            while current and current not in visited:
                visited.add(current)
                names.append(entity_names.get(current, current))
                current = location_parent.get(current)
            names.reverse()
            return names

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
        if scene_ids:
            link_rows = (
                await self._session.execute(
                    select(SceneLocationLink).where(
                        SceneLocationLink.scene_id.in_(scene_ids)
                    )
                )
            ).scalars().all()
            scene_locations = {
                row.scene_id: row.location_id for row in link_rows
            }

        scene_participants: dict[str, list[str]] = {scene_id: [] for scene_id in scene_ids}
        if scene_ids:
            participant_rows = (
                await self._session.execute(
                    select(SceneParticipant).where(
                        SceneParticipant.scene_id.in_(scene_ids)
                    )
                )
            ).scalars().all()
            for participant in participant_rows:
                scene_participants.setdefault(participant.scene_id, []).append(
                    participant.entity_id
                )

        active_scenes = [row for row in scenes if row.status == "active"]
        current_scene = scene_map.get(campaign.current_scene_id)
        scene_state_issues: list[str] = []
        if campaign.current_scene_id and current_scene is None:
            scene_state_issues.append("campaign.current_scene_id points to a missing scene")
        if current_scene and current_scene.status != "active":
            scene_state_issues.append(
                f"current scene has status '{current_scene.status}' instead of 'active'"
            )
        if scenes and not campaign.current_scene_id:
            scene_state_issues.append("campaign has scenes but current_scene_id is not set")
        if len(active_scenes) > 1:
            scene_state_issues.append(
                f"campaign has {len(active_scenes)} active scenes; expected exactly one"
            )
        if len(active_scenes) == 1 and current_scene and active_scenes[0].id != current_scene.id:
            scene_state_issues.append("the sole active scene differs from current_scene_id")
        if scenes and not active_scenes:
            scene_state_issues.append("campaign has no active scene")

        player_location_id = None
        if campaign.player_character_id:
            player_details = await self._session.get(
                Character,
                campaign.player_character_id,
            )
            player_location_id = (
                player_details.current_location_id if player_details else None
            )

        location_state_issues: list[str] = []
        for scene_id, location_id in scene_locations.items():
            location_entity = entity_map.get(location_id)
            if not location_entity:
                location_state_issues.append(
                    f"scene {scene_id} points to a missing location {location_id}"
                )
            elif location_entity.entity_type != "location":
                location_state_issues.append(
                    f"scene {scene_id} points to non-location entity {location_id}"
                )
        current_scene_location_id = (
            scene_locations.get(current_scene.id) if current_scene else None
        )
        if (
            campaign.player_character_id
            and current_scene_location_id
            and player_location_id != current_scene_location_id
        ):
            location_state_issues.append(
                "player current_location_id differs from the active scene location"
            )

        turns = (
            await self._session.execute(
                select(Turn)
                .where(Turn.campaign_id == str(campaign_id))
                .order_by(Turn.created_at.desc())
                .limit(turn_limit)
            )
        ).scalars().all()
        turns.reverse()
        turn_ids = [row.id for row in turns]

        facts = (
            await self._session.execute(
                select(Fact)
                .where(Fact.campaign_id == str(campaign_id))
                .order_by(Fact.created_at)
            )
        ).scalars().all()
        beliefs = (
            await self._session.execute(
                select(Belief)
                .join(Entity, Entity.id == Belief.character_id)
                .where(Entity.campaign_id == str(campaign_id))
                .order_by(Belief.created_at)
            )
        ).scalars().all()
        relationships = (
            await self._session.execute(
                select(RelationshipAssertion)
                .where(RelationshipAssertion.campaign_id == str(campaign_id))
                .order_by(RelationshipAssertion.created_at)
            )
        ).scalars().all()
        events = (
            await self._session.execute(
                select(Event)
                .where(Event.campaign_id == str(campaign_id))
                .order_by(Event.created_at)
            )
        ).scalars().all()
        proposals = []
        if turn_ids:
            proposals = (
                await self._session.execute(
                    select(ProposedChange)
                    .where(ProposedChange.turn_id.in_(turn_ids))
                    .order_by(ProposedChange.created_at)
                )
            ).scalars().all()
        theses = []
        if scene_ids:
            theses = (
                await self._session.execute(
                    select(SceneThesis)
                    .where(SceneThesis.scene_id.in_(scene_ids))
                    .order_by(SceneThesis.created_at)
                )
            ).scalars().all()
        jobs = (
            await self._session.execute(
                select(PostTurnJob)
                .where(PostTurnJob.campaign_id == str(campaign_id))
                .order_by(PostTurnJob.created_at.desc())
                .limit(200)
            )
        ).scalars().all()
        runs = (
            await self._session.execute(
                select(GenerationRun)
                .where(GenerationRun.campaign_id == str(campaign_id))
                .order_by(GenerationRun.created_at.desc())
                .limit(100)
            )
        ).scalars().all()
        run_ids = [row.id for row in runs]
        lifecycle_rows = []
        if run_ids:
            lifecycle_rows = (
                await self._session.execute(
                    select(GenerationLifecycle).where(
                        GenerationLifecycle.generation_run_id.in_(run_ids)
                    )
                )
            ).scalars().all()
        lifecycle_map = {row.generation_run_id: row for row in lifecycle_rows}

        turn_map = {row.id: row for row in turns}

        def scene_payload(row: Scene) -> dict:
            participant_ids = scene_participants.get(row.id, [])
            location_id = scene_locations.get(row.id)
            return {
                "id": row.id,
                "title": row.title,
                "status": row.status,
                "location_id": location_id,
                "location_name": entity_names.get(location_id),
                "location_path": location_path(location_id),
                "location_description": row.location_description,
                "mood": row.mood,
                "tension": row.tension,
                "is_current": row.id == campaign.current_scene_id,
                "participant_ids": participant_ids,
                "participant_names": [
                    entity_names.get(entity_id, entity_id)
                    for entity_id in participant_ids
                ],
                "created_at": row.created_at.isoformat(),
                "updated_at": row.updated_at.isoformat(),
            }

        return {
            "campaign": {
                "id": campaign.id,
                "name": campaign.name,
                "current_scene_id": campaign.current_scene_id,
                "player_character_id": campaign.player_character_id,
                "player_character_name": entity_names.get(campaign.player_character_id),
                "player_location_id": player_location_id,
                "player_location_name": entity_names.get(player_location_id),
                "player_location_path": location_path(player_location_id),
            },
            "active_scene": scene_payload(current_scene) if current_scene else None,
            "scene_state_issues": scene_state_issues,
            "location_state_issues": location_state_issues,
            "locations": [
                {
                    "id": row.id,
                    "name": row.canonical_name,
                    "description": row.description,
                    "parent_location_id": location_parent.get(row.id),
                    "parent_location_name": entity_names.get(
                        location_parent.get(row.id)
                    ),
                    "path": location_path(row.id),
                    "geography": (
                        location_detail_map[row.id].geography
                        if row.id in location_detail_map
                        else None
                    ),
                    "atmosphere": (
                        location_detail_map[row.id].atmosphere
                        if row.id in location_detail_map
                        else None
                    ),
                }
                for row in entities
                if row.entity_type == "location"
            ],
            "entities": [
                {
                    "id": row.id,
                    "type": row.entity_type,
                    "name": row.canonical_name,
                    "aliases": _json(row.aliases, []),
                    "status": row.status,
                }
                for row in entities
            ],
            "scenes": [scene_payload(row) for row in scenes],
            "turns": [
                {
                    "id": row.id,
                    "scene_id": row.scene_id,
                    "scene_title": scene_map[row.scene_id].title if row.scene_id in scene_map else None,
                    "role": row.role,
                    "actor_id": row.acting_character_id,
                    "actor_name": entity_names.get(row.acting_character_id),
                    "content": row.content,
                    "status": row.status,
                    "parent_turn_id": row.parent_turn_id,
                    "context_snapshot": _json(row.context_snapshot, {}),
                    "created_at": row.created_at.isoformat(),
                }
                for row in turns
            ],
            "facts": [
                {
                    "id": row.id,
                    "subject": row.subject,
                    "predicate": row.predicate,
                    "object_value": row.object_value,
                    "truth_status": row.truth_status,
                    "visibility": row.visibility,
                    "is_current": row.is_current,
                    "source_turn_id": row.source_turn_id,
                    "superseded_by": row.superseded_by,
                }
                for row in facts
            ],
            "beliefs": [
                {
                    "id": row.id,
                    "character_id": row.character_id,
                    "character_name": entity_names.get(row.character_id),
                    "proposition": row.proposition,
                    "status": row.status,
                    "confidence": row.confidence,
                    "source_turn_id": row.source_turn_id,
                    "source_character_id": row.source_character_id,
                    "source_character_name": entity_names.get(row.source_character_id),
                    "is_current": row.is_current,
                    "superseded_by": row.superseded_by,
                }
                for row in beliefs
            ],
            "relationships": [
                {
                    "id": row.id,
                    "subject_id": row.subject_id,
                    "subject_name": entity_names.get(row.subject_id),
                    "object_id": row.object_id,
                    "object_name": entity_names.get(row.object_id),
                    "relation_type": row.relation_type,
                    "description": row.description,
                    "intensity": row.intensity,
                    "source_turn_id": row.source_turn_id,
                    "is_current": row.is_current,
                    "superseded_by": row.superseded_by,
                }
                for row in relationships
            ],
            "events": [
                {
                    "id": row.id,
                    "event_type": row.event_type,
                    "description": row.description,
                    "location_id": row.location_id,
                    "source_turns": _json(row.source_turns, []),
                    "created_at": row.created_at.isoformat(),
                }
                for row in events
            ],
            "theses": [
                {
                    "id": row.id,
                    "scene_id": row.scene_id,
                    "type": row.thesis_type,
                    "text": row.text,
                    "status": row.status,
                    "visibility": row.visibility,
                    "source_turn_id": row.source_turn_id,
                    "pinned": row.pinned,
                }
                for row in theses
            ],
            "proposals": [
                {
                    "id": row.id,
                    "turn_id": row.turn_id,
                    "turn_content": turn_map.get(row.turn_id).content if row.turn_id in turn_map else None,
                    "change_type": row.change_type,
                    "payload": _json(row.payload, {}),
                    "status": row.status,
                    "user_edit": _json(row.user_edit, None),
                    "created_at": row.created_at.isoformat(),
                }
                for row in proposals
            ],
            "post_turn_jobs": [
                {
                    "id": row.id,
                    "assistant_turn_id": row.assistant_turn_id,
                    "job_type": row.job_type,
                    "status": row.status,
                    "attempts": row.attempts,
                    "error": row.error,
                    "updated_at": row.updated_at.isoformat(),
                }
                for row in jobs
            ],
            "generation_runs": [
                {
                    "id": row.id,
                    "user_turn_id": row.user_turn_id,
                    "assistant_turn_id": row.assistant_turn_id,
                    "status": row.status,
                    "phase": (
                        lifecycle_map[row.id].phase if row.id in lifecycle_map else None
                    ),
                    "attempt": (
                        lifecycle_map[row.id].attempt if row.id in lifecycle_map else None
                    ),
                    "phase_timestamps": (
                        {
                            "received": _iso(lifecycle_map[row.id].received_at),
                            "planned": _iso(lifecycle_map[row.id].planned_at),
                            "prepared": _iso(lifecycle_map[row.id].prepared_at),
                            "narrated": _iso(lifecycle_map[row.id].narrated_at),
                            "published": _iso(lifecycle_map[row.id].published_at),
                            "post_turn_done": _iso(lifecycle_map[row.id].post_turn_done_at),
                            "compensated": _iso(lifecycle_map[row.id].compensated_at),
                        }
                        if row.id in lifecycle_map
                        else {}
                    ),
                    "cancel_requested": row.cancel_requested,
                    "error": row.error,
                    "updated_at": row.updated_at.isoformat(),
                }
                for row in runs
            ],
            "health": {
                "scene_state_errors": len(scene_state_issues),
                "location_state_errors": len(location_state_issues),
                "canon_gaps": sum(
                    1
                    for row in proposals
                    if row.change_type == "canon_gap" and row.status != "rejected"
                ),
                "failed_jobs": sum(1 for row in jobs if row.status == "failed"),
                "pending_jobs": sum(1 for row in jobs if row.status == "pending"),
                "running_generations": sum(1 for row in runs if row.status == "running"),
                "dangerous_incomplete_generations": sum(
                    1
                    for row in lifecycle_rows
                    if row.phase in {"prepared", "narrated"}
                ),
            },
        }
