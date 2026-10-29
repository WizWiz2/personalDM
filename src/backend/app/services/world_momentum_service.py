"""Ground independent story developments in published canon and NPC goals.

Read-only selection: this service offers possible sources and a pacing signal.
It NEVER moves characters or changes facts. A separate authority gate validates
the selected event before narration and the world initiative ledger publishes it.
"""
from __future__ import annotations

import json
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.scene_repo import SceneRepository
from app.db.repositories.campaign_setup_repo import CampaignSetupRepository
from app.models.session_zero_interview import SessionZeroDeadline
from app.services.scene_state_service import SceneStateService
from app.db.scene_state_table import SceneRuntimeState
from app.db.tables import (
    Campaign, Character, CharacterGoal, Entity, Event, Scene, SceneParticipant,
    Turn,
)


class WorldMomentumService:
    QUIET_TURNS_THRESHOLD = 6
    MAX_THREAD_SOURCES = 12

    def __init__(self, session: AsyncSession):
        self._session = session

    async def _scheduled_deadlines(self, campaign_id: UUID) -> list[SessionZeroDeadline]:
        setups = CampaignSetupRepository(self._session)
        setup = await setups.get(campaign_id)
        custom = setups.decode_dict(setup.custom_fields) if setup else {}
        result = []
        for raw in (custom.get("scheduled_deadlines") or [])[:4]:
            try:
                result.append(SessionZeroDeadline.model_validate(raw))
            except (ValueError, TypeError):
                continue
        return result

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
        # Deadlines outrank optional actor threads and stale scene notes, and
        # a completed deadline must never be fed back as an active pressure.
        for deadline in await self._scheduled_deadlines(campaign_id):
            if await self.deadline_already_triggered(campaign_id, deadline.source):
                continue
            items.append({
                "source": deadline.source,
                "actor_id": None,
                "actor_name": None,
                "actor_location_id": None,
                "offscreen": False,
                "due_phase": deadline.due_phase,
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
                # Encountered actors, and people someone was sent to fetch (offscreen errand targets).
                or_(Entity.id.in_(seen_ids), func.json_extract(Entity.custom_fields, "$.sent_for").is_(True)),
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
        # Read active published authority, not derived Event projections which a replay may
        # rebuild. Undo deactivates the source turn and therefore reopens its agendas.
        closed = set()
        for raw in (await self._session.execute(select(func.json_extract(
            Turn.context_snapshot, "$.turn_authority.world_thread_closures",
        )).where(Turn.campaign_id == str(campaign_id), Turn.role == "assistant",
                 Turn.status == "active", Turn.context_snapshot.is_not(None)))).scalars():
            if not raw:
                continue
            for record in json.loads(raw):
                closed.add((record["source"].casefold().strip(), record.get("actor_id")))
        items = [item for item in items if (item["source"].casefold().strip(), item["actor_id"]) not in closed]
        # Deduplicate identical source+actor references without reordering.
        sources = list({
            (item["source"].casefold(), item["actor_id"]): item
            for item in items if item["source"].strip()
        }.values())[:self.MAX_THREAD_SOURCES]
        routes: dict[str, list[dict]] = {}
        for item in sources:
            origin = item["actor_location_id"]
            if origin:
                if origin not in routes:
                    exits = await SceneStateService(self._session).list_exits(
                        campaign_id, UUID(origin),
                    )
                    routes[origin] = [
                        {"destination_location_id": str(edge.to_location_id),
                         "name": edge.to_location_name}
                        for edge in exits if edge.active and edge.discovered
                    ]
                item["available_destinations"] = routes[origin]
        return sources

    async def snapshot(
        self, campaign_id: UUID, scene_id: UUID | None, *, patience: int | None = None,
    ) -> dict:
        """``patience``: the master's quiet published turns before the world must act."""
        patience = patience or self.QUIET_TURNS_THRESHOLD
        threads = await self.threads(campaign_id, scene_id)
        if not threads:
            return {"sources": [], "pressure_due": False, "quiet_turns": 0,
                    "world_time_label": None, "automatic_deadline": None}
        for index, thread in enumerate(threads):
            thread["thread_id"] = f"thread_{index}"
        last_independent, last_kind = (await self._session.execute(
            select(Event.created_at, Event.event_type).where(
                Event.campaign_id == str(campaign_id),
                Event.event_type.like("world_initiative:%"),
            ).order_by(Event.created_at.desc()).limit(1)
        )).first() or (None, None)
        published = select(Turn.created_at).where(
            Turn.campaign_id == str(campaign_id),
            Turn.role == "assistant",
            Turn.status == "active",
        )
        if last_independent is not None:
            published = published.where(Turn.created_at > last_independent)
        quiet_turns = len((await self._session.execute(
            published.order_by(Turn.created_at.desc()).limit(patience)
        )).scalars().all())
        runtime = (
            await self._session.get(SceneRuntimeState, str(scene_id))
            if scene_id else None
        )
        phase = SceneStateService.normalize_day_part(
            runtime.world_time_label if runtime else None
        )
        due = None
        for deadline in await self._scheduled_deadlines(campaign_id):
            if (
                deadline.due_phase == phase
                and not await self.deadline_already_triggered(
                    campaign_id, deadline.source,
                )
            ):
                due = {
                    "kind": "deadline",
                    "source": deadline.source,
                    "outcome": deadline.consequence,
                    "due_phase": deadline.due_phase,
                }
                break
        return {
            "sources": threads,
            "pressure_due": quiet_turns >= patience,
            "quiet_turns": quiet_turns,
            "last_world_move_kind": last_kind.removeprefix("world_initiative:") if last_kind else None,
            "world_time_label": phase,
            "automatic_deadline": due,
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
