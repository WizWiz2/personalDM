from uuid import UUID

from sqlalchemy import delete, select, update

from app.db.repositories.base import BaseRepository
from app.db.tables import Campaign, Event
from app.db.truth_engine_table import (
    AssertionSupport,
    EntityMention,
    FluentAssertion,
    TruthEventEffect,
    TruthEventEvidence,
    TruthEventRecord,
    WorldRelationAssertion,
)
from app.models.campaign import CampaignCreate, CampaignRead, CampaignUpdate


class CampaignRepository(BaseRepository):
    async def create(self, campaign_id: UUID, data: CampaignCreate) -> CampaignRead:
        db_campaign = Campaign(
            id=str(campaign_id),
            name=data.name,
            description=data.description,
            system_instructions=data.system_instructions,
            narrative_style=data.narrative_style,
            player_character_id=(str(data.player_character_id) if data.player_character_id else None),
        )
        self._session.add(db_campaign)
        await self._session.flush()
        return CampaignRead.model_validate(db_campaign)

    async def get_by_id(self, campaign_id: UUID) -> CampaignRead | None:
        result = await self._session.execute(
            select(Campaign).where(Campaign.id == str(campaign_id))
        )
        db_campaign = result.scalar_one_or_none()
        if not db_campaign:
            return None
        return CampaignRead.model_validate(db_campaign)

    async def list_all(self) -> list[CampaignRead]:
        result = await self._session.execute(
            select(Campaign).order_by(Campaign.created_at.desc())
        )
        campaigns = result.scalars().all()
        return [CampaignRead.model_validate(c) for c in campaigns]

    async def update(self, campaign_id: UUID, data: CampaignUpdate) -> CampaignRead | None:
        result = await self._session.execute(
            select(Campaign).where(Campaign.id == str(campaign_id))
        )
        db_campaign = result.scalar_one_or_none()
        if not db_campaign:
            return None

        update_data = data.model_dump(exclude_unset=True)
        for key, value in update_data.items():
            if key in {"current_scene_id", "player_character_id"} and value is not None:
                setattr(db_campaign, key, str(value))
            else:
                setattr(db_campaign, key, value)

        await self._session.flush()
        return CampaignRead.model_validate(db_campaign)

    async def delete(self, campaign_id: UUID) -> bool:
        """Delete a campaign and dependents that would block event CASCADE.

        Truth Engine fluent/world-relation assertions use ON DELETE RESTRICT on
        ``valid_from_event_id``. SQLAlchemy campaign→event cascade therefore fails
        with sqlite3.IntegrityError unless those rows are removed first. Keep the
        RESTRICT semantics for ordinary event deletes; only the campaign teardown
        path clears campaign-scoped truth rows in a safe order.
        """
        cid = str(campaign_id)
        result = await self._session.execute(
            select(Campaign).where(Campaign.id == cid)
        )
        db_campaign = result.scalar_one_or_none()
        if not db_campaign:
            return False

        # Break self-FKs on the campaign row before cascading children.
        await self._session.execute(
            update(Campaign)
            .where(Campaign.id == cid)
            .values(current_scene_id=None, player_character_id=None)
        )

        event_ids = select(Event.id).where(Event.campaign_id == cid)

        # RESTRICT holders first, then other event-linked truth rows.
        for table in (
            AssertionSupport,
            FluentAssertion,
            WorldRelationAssertion,
            EntityMention,
            TruthEventRecord,
        ):
            await self._session.execute(delete(table).where(table.campaign_id == cid))

        for table in (TruthEventEffect, TruthEventEvidence):
            await self._session.execute(delete(table).where(table.event_id.in_(event_ids)))

        await self._session.delete(db_campaign)
        await self._session.flush()
        return True
