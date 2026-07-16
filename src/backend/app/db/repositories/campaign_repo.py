from uuid import UUID
from sqlalchemy import select, update, delete
from app.db.repositories.base import BaseRepository
from app.db.tables import Campaign
from app.models.campaign import CampaignCreate, CampaignRead, CampaignUpdate

class CampaignRepository(BaseRepository):
    async def create(self, campaign_id: UUID, data: CampaignCreate) -> CampaignRead:
        db_campaign = Campaign(
            id=str(campaign_id),
            name=data.name,
            description=data.description,
            system_instructions=data.system_instructions,
            narrative_style=data.narrative_style
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
            if key == "current_scene_id" and value is not None:
                setattr(db_campaign, key, str(value))
            else:
                setattr(db_campaign, key, value)
                
        await self._session.flush()
        return CampaignRead.model_validate(db_campaign)

    async def delete(self, campaign_id: UUID) -> bool:
        result = await self._session.execute(
            select(Campaign).where(Campaign.id == str(campaign_id))
        )
        db_campaign = result.scalar_one_or_none()
        if not db_campaign:
            return False
            
        await self._session.delete(db_campaign)
        await self._session.flush()
        return True
