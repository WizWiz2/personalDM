import uuid
from uuid import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.provider_config_repo import ProviderConfigRepository
from app.models.campaign import CampaignCreate, CampaignRead, CampaignUpdate
from app.models.provider_config import ProviderConfigCreate, ProviderConfigRead
from app.providers.llm_provider import LLMProvider

class CampaignService:
    def __init__(self, session: AsyncSession):
        self._session = session
        self._campaign_repo = CampaignRepository(session)
        self._config_repo = ProviderConfigRepository(session)
        self._llm_provider = LLMProvider()

    async def create_campaign(self, data: CampaignCreate) -> CampaignRead:
        campaign_id = uuid.uuid4()
        # Create campaign row
        campaign = await self._campaign_repo.create(campaign_id, data)
        
        # Create default provider config (Ollama defaults)
        default_config = ProviderConfigCreate(
            base_url="http://localhost:11434/v1",
            model_name="gemma2:27b",
            api_key=None,
            context_window=8192
        )
        await self._config_repo.create_or_update(campaign_id, default_config)
        return campaign

    async def get_campaign(self, campaign_id: UUID) -> CampaignRead | None:
        return await self._campaign_repo.get_by_id(campaign_id)

    async def list_campaigns(self) -> list[CampaignRead]:
        return await self._campaign_repo.list_all()

    async def update_campaign(self, campaign_id: UUID, data: CampaignUpdate) -> CampaignRead | None:
        return await self._campaign_repo.update(campaign_id, data)

    async def delete_campaign(self, campaign_id: UUID) -> bool:
        return await self._campaign_repo.delete(campaign_id)

    # --- PROVIDER CONFIGS ---
    async def configure_provider(self, campaign_id: UUID, data: ProviderConfigCreate) -> ProviderConfigRead:
        # Verify campaign exists
        campaign = await self._campaign_repo.get_by_id(campaign_id)
        if not campaign:
            raise ValueError(f"Campaign {campaign_id} not found")
        return await self._config_repo.create_or_update(campaign_id, data)

    async def get_provider_config(self, campaign_id: UUID) -> ProviderConfigRead | None:
        return await self._config_repo.get_by_campaign_id(campaign_id)

    async def check_provider_connection(self, campaign_id: UUID) -> bool:
        config = await self._config_repo.get_by_campaign_id(campaign_id)
        if not config:
            return False
            
        api_key = await self._config_repo.get_decrypted_key(campaign_id)
        return await self._llm_provider.check_connection(
            base_url=config.base_url,
            model_name=config.model_name,
            api_key=api_key
        )
