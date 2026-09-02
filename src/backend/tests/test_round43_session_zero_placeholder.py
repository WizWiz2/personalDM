from unittest.mock import AsyncMock, patch

import pytest

from app.db.repositories.location_repo import LocationRepository
from app.models.campaign import CampaignCreate
from app.runtime import install_runtime
from app.services.campaign_service import CampaignService
from app.services.session_zero_agent import SessionZeroInterviewIncompleteError
from app.services.session_zero_interview import SessionZeroInterviewService
from app.services.session_zero_placeholder_guard import is_placeholder_location


def test_placeholder_location_classifier_is_narrow():
    assert is_placeholder_location("Стартовая локация") is True
    assert is_placeholder_location("Неизвестная локация") is True
    assert is_placeholder_location("Ночной рынок Редмонда") is False
    assert is_placeholder_location("Таверна «Старый мост»") is False


@pytest.mark.asyncio
async def test_finalize_never_materializes_starting_location_placeholder(db_session):
    install_runtime()
    campaign = await CampaignService(db_session).create_campaign(
        CampaignCreate(name="Placeholder protection")
    )
    interview = SessionZeroInterviewService(db_session)
    state = await interview.get_state(campaign.id)
    state.draft.world.starting_location_name = "Стартовая локация"
    state.draft.world.starting_situation = "Герою приносят письмо с первой зацепкой."
    state.draft.world.setting_name = "Городское фэнтези"
    state.draft.character.name = "Илья"
    state.draft.character.description = "Частный сыщик, знакомый с миром магии."
    state.draft.character.first_goal = "Узнать, кто отправил письмо."
    await interview._save_state(campaign.id, state, commit=True)

    with (
        patch.object(
            interview._router,
            "resolve",
            new_callable=AsyncMock,
            return_value=None,
        ),
        pytest.raises(SessionZeroInterviewIncompleteError) as exc,
    ):
        await interview.finalize(campaign.id)

    assert "world.starting_location_name" in exc.value.missing_fields
    assert await LocationRepository(db_session).list_by_campaign(campaign.id) == []

    state = await interview.get_state(campaign.id)
    assert state.draft.world.starting_location_name is None
