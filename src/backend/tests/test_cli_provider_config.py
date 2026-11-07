from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import cli
from app.db.engine import Base
from app.db.tables import ProviderConfig
from app.models.campaign import CampaignCreate
from app.models.provider_config import ProviderConfigCreate
from app.services.campaign_service import CampaignService


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_kind", ["chatgpt", "openai_compatible"])
@pytest.mark.parametrize("edited", [False, True])
async def test_cli_provider_edit_preserves_saved_transport_and_unedited_settings(
    tmp_path, monkeypatch, provider_kind, edited,
):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'provider.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(cli.settings, "LLM_BASE_URL", "http://global.invalid/v1")
    monkeypatch.setattr(cli.settings, "LLM_MODEL", "global-model")
    monkeypatch.setattr(cli.settings, "LLM_CONTEXT_WINDOW", 4096)
    monkeypatch.setattr(cli.settings, "LLM_API_KEY", "synthetic-global-test-key")
    monkeypatch.setattr(cli.settings, "TEXT_PROVIDER", "local")
    initial_url = "http://campaign.invalid/v1"
    initial_model = "campaign-model"
    async with sessions() as session:
        service = CampaignService(session)
        campaign = await service.create_campaign(CampaignCreate(name="CLI settings"))
        campaign_id = campaign.id
        await service.configure_provider(campaign_id, ProviderConfigCreate(
            base_url=initial_url, model_name=initial_model,
            provider_kind=provider_kind, context_window=32768,
            api_key="synthetic-campaign-test-key",
        ))
        await session.commit()
        original_key = await session.scalar(select(ProviderConfig.api_key_encrypted))
        answers = ["http://edited.invalid/v1", "edited-model", "", "65536"] if edited else [""] * 4
        with patch("builtins.input", side_effect=answers):
            await cli.configure_llm_menu(campaign_id, service, session)
    async with sessions() as verification_session:
        stored = await CampaignService(verification_session).get_provider_config(campaign_id)
        saved_key = await verification_session.scalar(select(ProviderConfig.api_key_encrypted))
        actual = (stored.provider_kind, stored.base_url, stored.model_name, stored.context_window)
    await engine.dispose()
    expected = (provider_kind, "http://edited.invalid/v1", "edited-model", 65536) if edited else (
        provider_kind, initial_url, initial_model, 32768,
    )
    assert actual == expected
    assert saved_key == original_key
