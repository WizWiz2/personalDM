import json
from uuid import UUID
from sqlalchemy import select
from app.db.repositories.base import BaseRepository
from app.db.tables import Event, EventParticipant
from app.models.event import EventCreate, EventRead

class EventRepository(BaseRepository):
    async def create(self, campaign_id: UUID, data: EventCreate, source_turns: list[UUID] | None = None) -> EventRead:
        turns_json = json.dumps([str(t) for t in source_turns]) if source_turns else json.dumps([])
        db_event = Event(
            campaign_id=str(campaign_id),
            event_type=data.event_type,
            description=data.description,
            world_time=data.world_time,
            location_id=str(data.location_id) if data.location_id else None,
            importance=data.importance,
            source_turns=turns_json
        )
        self._session.add(db_event)
        await self._session.flush()

        # Add participants
        for part_id in data.participant_ids:
            db_part = EventParticipant(
                event_id=db_event.id,
                entity_id=str(part_id)
            )
            self._session.add(db_part)
            
        await self._session.flush()
        return await self.get_by_id(UUID(db_event.id))

    async def get_by_id(self, event_id: UUID) -> EventRead | None:
        result = await self._session.execute(
            select(Event).where(Event.id == str(event_id))
        )
        db_event = result.scalar_one_or_none()
        if not db_event:
            return None

        # Get participants
        p_result = await self._session.execute(
            select(EventParticipant.entity_id).where(EventParticipant.event_id == str(event_id))
        )
        participant_ids = [UUID(pid) for pid in p_result.scalars().all()]
        
        # Parse source turns
        source_turns = []
        if db_event.source_turns:
            try:
                source_turns = [UUID(t) for t in json.loads(db_event.source_turns)]
            except Exception:
                pass

        event_read = EventRead.model_validate(db_event)
        event_read.participant_ids = participant_ids
        event_read.source_turns = source_turns
        return event_read

    async def list_by_campaign(self, campaign_id: UUID, importance: str | None = None) -> list[EventRead]:
        query = select(Event).where(Event.campaign_id == str(campaign_id))
        if importance:
            query = query.where(Event.importance == importance)
        query = query.order_by(Event.created_at.desc())
        
        result = await self._session.execute(query)
        events = result.scalars().all()
        
        results = []
        for e in events:
            ev_read = await self.get_by_id(UUID(e.id))
            if ev_read:
                results.append(ev_read)
        return results
