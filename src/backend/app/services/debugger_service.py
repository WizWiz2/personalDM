import json
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import (
    Belief,
    Campaign,
    Entity,
    Event,
    Fact,
    GenerationRun,
    PostTurnJob,
    ProposedChange,
    RelationshipAssertion,
    Scene,
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

        scenes = (
            await self._session.execute(
                select(Scene)
                .where(Scene.campaign_id == str(campaign_id))
                .order_by(Scene.created_at)
            )
        ).scalars().all()
        scene_ids = [row.id for row in scenes]

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

        turn_map = {row.id: row for row in turns}
        return {
            "campaign": {
                "id": campaign.id,
                "name": campaign.name,
                "current_scene_id": campaign.current_scene_id,
                "player_character_id": campaign.player_character_id,
                "player_character_name": entity_names.get(campaign.player_character_id),
            },
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
            "scenes": [
                {
                    "id": row.id,
                    "title": row.title,
                    "status": row.status,
                    "mood": row.mood,
                    "tension": row.tension,
                }
                for row in scenes
            ],
            "turns": [
                {
                    "id": row.id,
                    "scene_id": row.scene_id,
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
                    "cancel_requested": row.cancel_requested,
                    "error": row.error,
                    "updated_at": row.updated_at.isoformat(),
                }
                for row in runs
            ],
            "health": {
                "canon_gaps": sum(
                    1
                    for row in proposals
                    if row.change_type == "canon_gap" and row.status != "rejected"
                ),
                "failed_jobs": sum(1 for row in jobs if row.status == "failed"),
                "pending_jobs": sum(1 for row in jobs if row.status == "pending"),
                "running_generations": sum(1 for row in runs if row.status == "running"),
            },
        }
