"""Ground independent story developments in published canon and NPC goals.

Read-only selection: this service offers possible sources and a pacing signal.
It NEVER moves characters or changes facts. A separate authority gate validates
the selected event before narration and the world initiative ledger publishes it.
"""
from __future__ import annotations

import json
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.scene_repo import SceneRepository
from app.db.scene_state_table import SceneRuntimeState
from app.db.tables import (
    Campaign, Character, CharacterGoal, Entity, Event, Scene, SceneParticipant,
    Turn,
)


class WorldMomentumService:
    QUIET_TURNS_THRESHOLD = 6

    def __init__(self, session: AsyncSession):
        self._session = session

    async def threads(self, campaign_id: UUID, scene_id: UUID | None) -> list[dict]:
        if scene_id is None:
            return []
        campaign = await self._session.get(Campaign, str(campaign_id))
        if not campaign:
            return []
        items: list[dict] = []
        runtime = await self._session.get(SceneRuntimeState, str(scene_id))
        if runtime and runtime.active_conflict:
            items.append({
                "source": runtime.active_conflict,
                "actor_id": None,
                "actor_name": None,
                "actor_location_id": None,
                "offscreen": False,
            })
        theses = await SceneRepository(self._session).list_theses_by_scene(
            scene_id, active_only=True,
        )
        for thesis in theses:
            if thesis.thesis_type in {"tension", "unresolved_beat"}:
                items.append({
                    "source": thesis.text,
                    "actor_id": None,
                    "actor_name": None,
                    "actor_location_id": None,
                    "offscreen": False,
                })

        current_scene = await SceneRepository(self._session).get_by_id(scene_id)
        current_place_id = str(current_scene.location_id) if current_scene and current_scene.location_id else None
        # Only previously encountered actors, not every hidden NPC in the DB.
        seen_ids = select(SceneParticipant.entity_id).join(
            Scene, SceneParticipant.scene_id == Scene.id,
        ).where(Scene.campaign_id == str(campaign_id))
        rows = await self._session.execute(
            select(CharacterGoal, Entity, Character)
            .join(Entity, Entity.id == CharacterGoal.character_id)
            .join(Character, Character.entity_id == Entity.id)
            .where(
                Entity.campaign_id == str(campaign_id),
                Entity.entity_type == "character",
                CharacterGoal.status == "active",
                CharacterGoal.is_secret.is_(False),
                Entity.id != (campaign.player_character_id or ""),
                Entity.id.in_(seen_ids),
                Character.current_location_id.is_not(None),
            )
            .order_by(CharacterGoal.priority.desc(), CharacterGoal.id)
            .limit(12)
        )
        for goal, entity, character in rows.all():
            items.append({
                "source": goal.description,
                "actor_id": entity.id,
                "actor_name": entity.canonical_name,
                "actor_location_id": character.current_location_id,
                "offscreen": character.current_location_id != current_place_id,
            })
        # Character cards already store short-term intentions. Use them even
        # when the richer CharacterGoal table has no rows (common for NPCs
        # introduced mid-game). Never invent an agenda from their job title.
        actor_rows = await self._session.execute(
            select(Entity, Character)
            .join(Character, Character.entity_id == Entity.id)
            .where(
                Entity.campaign_id == str(campaign_id),
                Entity.entity_type == "character",
                Entity.id != (campaign.player_character_id or ""),
                Entity.id.in_(seen_ids),
                Character.current_location_id.is_not(None),
            )
            .order_by(Entity.canonical_name)
            .limit(25)
        )
        for entity, character in actor_rows.all():
            try:
                intentions = json.loads(character.current_intentions or "[]")
            except (ValueError, TypeError):
                intentions = []
            if not isinstance(intentions, list):
                continue
            for statement in intentions[:2]:
                if not isinstance(statement, str) or len(statement.strip()) < 8:
                    continue
                items.append({
                    "source": statement.strip(),
                    "actor_id": entity.id,
                    "actor_name": entity.canonical_name,
                    "actor_location_id": character.current_location_id,
                    "offscreen": character.current_location_id != current_place_id,
                })
        # Deduplicate identical source+actor references without reordering.
        return list({
            (item["source"].casefold(), item["actor_id"]): item
            for item in items if item["source"].strip()
        }.values())

    async def snapshot(self, campaign_id: UUID, scene_id: UUID | None) -> dict:
        threads = await self.threads(campaign_id, scene_id)
        if not threads:
            return {"sources": [], "pressure_due": False, "quiet_turns": 0,
                    "world_time_label": None}
        rows = (
            await self._session.execute(
                select(Turn.created_at).where(
                    Turn.campaign_id == str(campaign_id),
                    Turn.role == "assistant",
                    Turn.status == "active",
                ).order_by(Turn.created_at.desc()).limit(self.QUIET_TURNS_THRESHOLD)
            )
        ).scalars().all()
        quiet_turns = len(rows)
        if rows:
            last_independent = await self._session.scalar(
                select(Event.id).where(
                    Event.campaign_id == str(campaign_id),
                    Event.event_type.like("world_initiative:%"),
                    Event.created_at >= rows[-1],
                ).limit(1)
            )
        else:
            last_independent = None
        runtime = (
            await self._session.get(SceneRuntimeState, str(scene_id))
            if scene_id else None
        )
        return {
            "sources": threads,
            "pressure_due": len(rows) >= self.QUIET_TURNS_THRESHOLD
                            and last_independent is None,
            "quiet_turns": quiet_turns,
            "world_time_label": runtime.world_time_label if runtime else None,
        }

    async def deadline_already_triggered(
        self, campaign_id: UUID, source: str,
    ) -> bool:
        # Assistant-bound world events are retractable on /undo; a deadline is
        # one-shot while its outcome remains published.
        rows = (
            await self._session.execute(
                select(Event.description).where(
                    Event.campaign_id == str(campaign_id),
                    Event.event_type == "world_initiative:deadline",
                )
            )
        ).scalars().all()
        return any(text.startswith(source + "\n") for text in rows)
