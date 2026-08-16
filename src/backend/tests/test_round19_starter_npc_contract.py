from unittest.mock import AsyncMock, patch

import pytest

from app.models.campaign import CampaignCreate
from app.services.campaign_service import CampaignService
from app.services.playable_bootstrap import PlayableBootstrapService
from app.services.scene_state_service import SceneStateService
from app.services.session_zero_interview import SessionZeroInterviewService


ROUND19_START = {
    "world": {
        "setting_name": "Современный портовый город",
        "genre": "детективное приключение",
        "tone": "приземлённый",
        "starting_location_name": "Приёмная частного детектива",
        "starting_situation": "Получить простой оплачиваемый заказ для своей приёмной.",
    },
    "character": {
        "name": "Мария Ивановна",
        "description": "Частный детектив, которая берётся за небольшие портовые дела.",
        "first_goal": "Получить оплачиваемый заказ.",
    },
}


def test_job_shaped_start_requires_one_mundane_contact():
    situation = ROUND19_START["world"]["starting_situation"]

    assert PlayableBootstrapService._mentions_contact(situation)
    assert PlayableBootstrapService._contact_identity(situation) == (
        "Заказчик",
        "заказчик",
    )


@pytest.mark.asyncio
async def test_round19_job_start_materializes_customer_before_gameplay(db_session):
    campaign = await CampaignService(db_session).create_campaign(
        CampaignCreate(name="Round 19 starter NPC regression")
    )
    await db_session.commit()
    interview = SessionZeroInterviewService(db_session)
    response = {
        "assistant_message": (
            "Начинаем в вашей приёмной: к вам приходит человек с небольшим оплачиваемым делом."
        ),
        "tool_calls": [
            {"name": "update_session_zero", "patch": ROUND19_START},
            {"name": "finalize_session_zero"},
        ],
        "question_topics": [],
    }

    with patch(
        "app.services.session_zero_interview.RoleModelRouter.generate_json",
        new_callable=AsyncMock,
        return_value=response,
    ):
        decision = await interview.answer(
            campaign.id,
            "Хочу начать с обычного оплачиваемого заказа в своей приёмной.",
        )

    assert decision.ready_to_finalize is True
    completed = await interview.finalize(campaign.id)
    state = await SceneStateService(db_session).get(campaign.id, completed.scene.id)

    assert state.valid is True
    assert "Мария Ивановна" in state.participant_names
    assert "Заказчик" in state.participant_names
    assert len([name for name in state.participant_names if name != "Мария Ивановна"]) == 1

    customer_id = next(
        entity_id
        for entity_id, name in zip(state.participant_ids, state.participant_names)
        if name == "Заказчик"
    )
    assert customer_id != completed.setup.player_character_id
