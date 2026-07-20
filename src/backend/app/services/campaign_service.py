import uuid
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.provider_config_repo import ProviderConfigRepository
from app.db.repositories.entity_repo import EntityRepository
from app.models.campaign import CampaignCreate, CampaignRead, CampaignUpdate
from app.models.provider_config import ProviderConfigCreate, ProviderConfigRead
from app.providers.llm_provider import LLMProvider


class CampaignService:
    def __init__(self, session: AsyncSession):
        self._session = session
        self._campaign_repo = CampaignRepository(session)
        self._config_repo = ProviderConfigRepository(session)
        self._entity_repo = EntityRepository(session)
        self._llm_provider = LLMProvider()

    async def create_campaign(self, data: CampaignCreate) -> CampaignRead:
        campaign_id = uuid.uuid4()
        campaign = await self._campaign_repo.create(campaign_id, data)
        await self._config_repo.create_or_update(
            campaign_id,
            ProviderConfigCreate(
                base_url=settings.LLM_BASE_URL,
                model_name=settings.LLM_MODEL,
                api_key=settings.LLM_API_KEY,
                context_window=settings.LLM_CONTEXT_WINDOW,
            ),
        )
        return campaign

    async def get_campaign(self, campaign_id: UUID) -> CampaignRead | None:
        return await self._campaign_repo.get_by_id(campaign_id)

    async def list_campaigns(self) -> list[CampaignRead]:
        return await self._campaign_repo.list_all()

    async def update_campaign(
        self,
        campaign_id: UUID,
        data: CampaignUpdate,
    ) -> CampaignRead | None:
        if data.player_character_id is not None:
            entity = await self._entity_repo.get_by_id(data.player_character_id)
            if not entity or entity.campaign_id != campaign_id:
                raise ValueError("Player character must belong to this campaign")
            if entity.entity_type != "character":
                raise ValueError("Player character must reference a character entity")
        return await self._campaign_repo.update(campaign_id, data)

    async def delete_campaign(self, campaign_id: UUID) -> bool:
        return await self._campaign_repo.delete(campaign_id)

    async def configure_provider(
        self,
        campaign_id: UUID,
        data: ProviderConfigCreate,
    ) -> ProviderConfigRead:
        campaign = await self._campaign_repo.get_by_id(campaign_id)
        if not campaign:
            raise ValueError(f"Campaign {campaign_id} not found")
        return await self._config_repo.create_or_update(campaign_id, data)

    async def get_provider_config(
        self,
        campaign_id: UUID,
    ) -> ProviderConfigRead | None:
        return await self._config_repo.get_by_campaign_id(campaign_id)

    async def check_provider_connection(self, campaign_id: UUID) -> bool:
        config = await self._config_repo.get_by_campaign_id(campaign_id)
        if not config:
            return False
        api_key = await self._config_repo.get_decrypted_key(campaign_id)
        return await self._llm_provider.check_connection(
            base_url=config.base_url,
            model_name=config.model_name,
            api_key=api_key,
        )
