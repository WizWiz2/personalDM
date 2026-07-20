from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.engine import get_session
from app.models.campaign import CampaignCreate, CampaignRead, CampaignUpdate
from app.models.provider_config import ProviderConfigCreate, ProviderConfigRead
from app.services.campaign_service import CampaignService

router = APIRouter(prefix="/api/campaigns", tags=["campaigns"])

@router.post("", response_model=CampaignRead, status_code=status.HTTP_201_CREATED)
async def create_campaign(data: CampaignCreate, session: AsyncSession = Depends(get_session)):
    service = CampaignService(session)
    return await service.create_campaign(data)

@router.get("", response_model=list[CampaignRead])
async def list_campaigns(session: AsyncSession = Depends(get_session)):
    service = CampaignService(session)
    return await service.list_campaigns()

@router.get("/{campaign_id}", response_model=CampaignRead)
async def get_campaign(campaign_id: UUID, session: AsyncSession = Depends(get_session)):
    service = CampaignService(session)
    campaign = await service.get_campaign(campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return campaign

@router.put("/{campaign_id}", response_model=CampaignRead)
async def update_campaign(campaign_id: UUID, data: CampaignUpdate, session: AsyncSession = Depends(get_session)):
    service = CampaignService(session)
    try:
        campaign = await service.update_campaign(campaign_id, data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return campaign

@router.delete("/{campaign_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_campaign(campaign_id: UUID, session: AsyncSession = Depends(get_session)):
    service = CampaignService(session)
    success = await service.delete_campaign(campaign_id)
    if not success:
        raise HTTPException(status_code=404, detail="Campaign not found")

# --- PROVIDER CONFIGS ---

@router.post("/{campaign_id}/provider", response_model=ProviderConfigRead)
async def configure_provider(campaign_id: UUID, data: ProviderConfigCreate, session: AsyncSession = Depends(get_session)):
    service = CampaignService(session)
    try:
        return await service.configure_provider(campaign_id, data)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

@router.get("/{campaign_id}/provider", response_model=ProviderConfigRead)
async def get_provider_config(campaign_id: UUID, session: AsyncSession = Depends(get_session)):
    service = CampaignService(session)
    config = await service.get_provider_config(campaign_id)
    if not config:
        raise HTTPException(status_code=404, detail="Provider config not found")
    return config

@router.post("/{campaign_id}/provider/check")
async def check_provider_connection(campaign_id: UUID, session: AsyncSession = Depends(get_session)):
    service = CampaignService(session)
    connected = await service.check_provider_connection(campaign_id)
    return {"connected": connected}
