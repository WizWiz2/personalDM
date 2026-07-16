from uuid import UUID
from sqlalchemy import select
from app.db.repositories.base import BaseRepository
from app.db.tables import ProviderConfig
from app.models.provider_config import ProviderConfigCreate, ProviderConfigRead, ProviderConfigUpdate
from app.services.security import encrypt_secret, decrypt_secret

class ProviderConfigRepository(BaseRepository):
    async def create_or_update(self, campaign_id: UUID, data: ProviderConfigCreate) -> ProviderConfigRead:
        result = await self._session.execute(
            select(ProviderConfig).where(ProviderConfig.campaign_id == str(campaign_id))
        )
        db_config = result.scalar_one_or_none()
        
        encrypted_key = encrypt_secret(data.api_key) if data.api_key else None
        
        if db_config:
            db_config.base_url = data.base_url
            db_config.model_name = data.model_name
            if data.api_key is not None:  # only update if provided
                db_config.api_key_encrypted = encrypted_key
            db_config.context_window = data.context_window
        else:
            db_config = ProviderConfig(
                campaign_id=str(campaign_id),
                base_url=data.base_url,
                model_name=data.model_name,
                api_key_encrypted=encrypted_key,
                context_window=data.context_window
            )
            self._session.add(db_config)
            
        await self._session.flush()
        return self._to_read_model(db_config)

    async def get_by_campaign_id(self, campaign_id: UUID) -> ProviderConfigRead | None:
        result = await self._session.execute(
            select(ProviderConfig).where(ProviderConfig.campaign_id == str(campaign_id))
        )
        db_config = result.scalar_one_or_none()
        if not db_config:
            return None
        return self._to_read_model(db_config)

    async def get_decrypted_key(self, campaign_id: UUID) -> str | None:
        result = await self._session.execute(
            select(ProviderConfig.api_key_encrypted).where(ProviderConfig.campaign_id == str(campaign_id))
        )
        encrypted_key = result.scalar()
        if not encrypted_key:
            return None
        return decrypt_secret(encrypted_key)

    def _to_read_model(self, db_config: ProviderConfig) -> ProviderConfigRead:
        has_key = db_config.api_key_encrypted is not None and len(db_config.api_key_encrypted) > 0
        return ProviderConfigRead(
            id=UUID(db_config.id),
            campaign_id=UUID(db_config.campaign_id),
            base_url=db_config.base_url,
            model_name=db_config.model_name,
            has_api_key=has_key,
            context_window=db_config.context_window,
            created_at=db_config.created_at
        )
