import json
from uuid import UUID

from sqlalchemy import or_, select

from app.db.repositories.base import BaseRepository
from app.db.tables import Event, EventParticipant, Turn
from app.db.truth_engine_table import TruthEventRecord
from app.models.event import EventCreate, EventRead


class EventRepository(BaseRepository):
    async def create(
        self,
        campaign_id: UUID,
        data: EventCreate,
        source_turns: list[UUID] | None = None,
    ) -> EventRead:
        effective_sources = source_turns if source_turns is not None else data.source_turns
        db_event = Event(
            campaign_id=str(campaign_id),
            event_type=data.event_type,
            description=data.description,
            world_time=data.world_time,
            location_id=str(data.location_id) if data.location_id else None,
            importance=data.importance,
            source_turns=json.dumps([str(turn_id) for turn_id in effective_sources]),
        )
        self._session.add(db_event)
        await self._session.flush()

        for participant_id in data.participant_ids:
            self._session.add(
                EventParticipant(
                    event_id=db_event.id,
                    entity_id=str(participant_id),
                )
            )
        await self._session.flush()
        return await self.get_by_id(UUID(db_event.id))

    async def get_by_id(self, event_id: UUID) -> EventRead | None:
        result = await self._session.execute(
            select(Event).where(Event.id == str(event_id))
        )
        db_event = result.scalar_one_or_none()
        if not db_event:
            return None
        truth_record = await self._session.get(TruthEventRecord, str(event_id))

        participants_result = await self._session.execute(
            select(EventParticipant.entity_id).where(
                EventParticipant.event_id == str(event_id)
            )
        )
        participant_ids = [
            UUID(participant_id)
            for participant_id in participants_result.scalars().all()
        ]

        source_turns: list[UUID] = []
        if db_event.source_turns:
            try:
                source_turns = [
                    UUID(turn_id)
                    for turn_id in json.loads(db_event.source_turns)
                ]
            except (ValueError, TypeError, json.JSONDecodeError):
                source_turns = []

        status = truth_record.status if truth_record else "active"
        if status == "active" and source_turns:
            sources = (await self._session.execute(select(Turn.status).where(
                Turn.campaign_id == db_event.campaign_id,
                Turn.id.in_([str(turn_id) for turn_id in source_turns]),
            ))).scalars().all()
            if "undone" in sources:
                status = "undone"
            elif any(source != "active" for source in sources):
                status = "inactive"

        return EventRead(
            id=UUID(db_event.id),
            campaign_id=UUID(db_event.campaign_id),
            # Public API keeps the legacy movement type; provenance remains
            # explicit in the persisted event for safe independent GM undo.
            event_type="movement" if db_event.event_type == "gm_movement" else db_event.event_type,
            status=status,
            description=db_event.description,
            world_time=db_event.world_time,
            location_id=(UUID(db_event.location_id) if db_event.location_id else None),
            importance=db_event.importance,
            source_turns=source_turns,
            participant_ids=participant_ids,
            created_at=db_event.created_at,
        )

    async def list_by_campaign(
        self,
        campaign_id: UUID,
        importance: str | None = None,
    ) -> list[EventRead]:
        query = select(Event).outerjoin(
            TruthEventRecord, TruthEventRecord.event_id == Event.id,
        ).where(
            Event.campaign_id == str(campaign_id),
            or_(TruthEventRecord.event_id.is_(None), TruthEventRecord.status == "active"),
        )
        if importance:
            query = query.where(Event.importance == importance)
        query = query.order_by(Event.created_at.desc())
        result = await self._session.execute(query)

        events = []
        for db_event in result.scalars().all():
            event = await self.get_by_id(UUID(db_event.id))
            if event and event.status == "active":
                events.append(event)
        return events
