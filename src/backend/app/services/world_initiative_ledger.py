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
from app.db.tables import Event
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
        location_id = (
            await SceneRepository(self._session).get_location_id(authority.target_scene_id)
            if authority.target_scene_id else None
        )
        created = await EventRepository(self._session).create(
            authority.campaign_id,
            EventCreate(
                event_type=f"world_initiative:{move.kind}",
                description=move.outcome,
                world_time=authority.scene_time,
                location_id=location_id,
                importance="normal",
            ),
            source_turns=[assistant_turn_id],
        )
        return created.id
