"""Record accepted world initiative as durable canon with assistant-turn provenance.

This ledger intentionally does not perform movement, custody, introduction,
damage or other physical changes; those need separate typed executors.
"""
from __future__ import annotations

import json
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.event_repo import EventRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.tables import Character, Event
from app.services.presence_service import PresenceService
from app.services.world_momentum_service import WorldMomentumService
from app.models.event import EventCreate
from app.models.turn_authority import TurnAuthority


class WorldInitiativeLedger:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def record(self, authority: TurnAuthority, assistant_turn_id: UUID) -> UUID | None:
        move = authority.world_move
        if not move:
            return None
        provenance = json.dumps([str(assistant_turn_id)])
        existing = await self._session.scalar(
            select(Event.id).where(
                Event.campaign_id == str(authority.campaign_id),
                Event.event_type.like("world_initiative:%"),
                Event.source_turns == provenance,
            )
        )
        if existing:
            return UUID(existing)
        if move.kind == "deadline" and await WorldMomentumService(
            self._session
        ).deadline_already_triggered(authority.campaign_id, move.source):
            return None  # A published deadline is one-shot until undone.
        location_id = (
            await SceneRepository(self._session).get_location_id(authority.target_scene_id)
            if authority.target_scene_id else None
        )
        if move.actor_id:
            actor = await self._session.get(Character, str(move.actor_id))
            if not actor or not actor.current_location_id:
                raise ValueError("Autonomous NPC has no authoritative physical position")
            if move.kind == "npc_move":
                if not move.destination_location_id:
                    raise ValueError("Typed autonomous NPC move has no destination")
                await PresenceService(self._session).move_to_location(
                    authority.campaign_id,
                    move.actor_id,
                    move.destination_location_id,
                    expected_origin=UUID(actor.current_location_id),
                )
            # Offscreen NPC events happen at that NPC's actual location.
            location_id = UUID(actor.current_location_id)
        created = await EventRepository(self._session).create(
            authority.campaign_id,
            EventCreate(
                event_type=f"world_initiative:{move.kind}",
                description=f"{move.source}\n{move.outcome}",
                world_time=authority.scene_time,
                location_id=location_id,
                participant_ids=[move.actor_id] if move.actor_id else [],
                importance="normal",
            ),
            source_turns=[assistant_turn_id],
        )
        return created.id
