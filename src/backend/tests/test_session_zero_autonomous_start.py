from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import cli
from app.models.campaign import CampaignCreate
from app.services.campaign_service import CampaignService
from app.services.session_zero_interview import SessionZeroInterviewService
from app.services.session_zero_service import SessionZeroService


MINIMAL_START_PATCH = {
    "world": {
        "setting_name": "Shadowrun",
        "genre": "киберпанк с магией",
        "starting_location_name": "Ночной рынок Редмонда",
        "starting_situation": (
            "Кабуто узнаёт о дешёвой кибердеке, которую этой ночью выставит "
            "на продажу подозрительный посредник."
        ),
        "starting_scene_title": "Сделка на ночном рынке",
    },
    "character": {
        "name": "Кабуто",
        "description": "Уличный маг, хакер и паркурщик в закрытом шлеме.",
        "first_goal": "Раздобыть нормальную кибердеку.",
    },
}


def _decision(
    message: str,
    *,
    patch_data: dict | None = None,
    finalize: bool = False,
):
    calls = []
    if patch_data is not None:
        calls.append({"name": "update_session_zero", "patch": patch_data})
    if finalize:
        calls.append({"name": "finalize_session_zero"})
    return {
        "assistant_message": message,
        "tool_calls": calls,
        "question_topics": [],
    }


async def _campaign(db_session: AsyncSession, name: str):
    campaign = await CampaignService(db_session).create_campaign(
        CampaignCreate(name=name)
    )
    await db_session.commit()
    return campaign


@pytest.mark.asyncio
async def test_agent_can_finalize_with_only_playable_critical_mass(
    db_session: AsyncSession,
):
    campaign = await _campaign(db_session, "Critical mass")
    interview = SessionZeroInterviewService(db_session)
    response = _decision(
        "Этого достаточно. Начинаем со сделки на ночном рынке.",
        patch_data=MINIMAL_START_PATCH,
        finalize=True,
    )

    with patch(
        "app.services.session_zero_interview.RoleModelRouter.generate_json",
        new_callable=AsyncMock,
        return_value=response,
    ):
        decision = await interview.answer(
            campaign.id,
            "Кабуто хочет найти хорошую деку. Остальное придумай сам, погнали.",
        )

    assert decision.ready_to_finalize is True
    assert decision.missing_topics == []
    assert decision.draft.character.values == []
    assert decision.draft.character.fears == []
    assert decision.draft.character.biography is None
    assert decision.draft.world.tone is None


@pytest.mark.asyncio
async def test_failed_finalize_is_repaired_by_agent_instead_of_more_questions(
    db_session: AsyncSession,
):
    campaign = await _campaign(db_session, "Invent missing start")
    interview = SessionZeroInterviewService(db_session)
    premature = _decision(
        "Погнали.",
        patch_data={
            "world": {"setting_name": "Shadowrun"},
            "character": {
                "name": "Кабуто",
                "description": "Уличный маг и хакер.",
            },
        },
        finalize=True,
    )
    repaired = _decision(
        "Начинаем: ночью Кабуто приходит на рынок за новой декой.",
        patch_data={
            "world": {
                "starting_location_name": "Ночной рынок Редмонда",
                "starting_situation": "Посредник предлагает Кабуто подозрительно дешёвую деку.",
            },
            "character": {"first_goal": "Раздобыть нормальную кибердеку."},
        },
        finalize=True,
    )
    model = AsyncMock(side_effect=[premature, repaired])

    with patch(
        "app.services.session_zero_interview.RoleModelRouter.generate_json",
        new=model,
    ):
        decision = await interview.answer(campaign.id, "Остальное придумай сам")

    assert model.await_count == 2
    assert decision.ready_to_finalize is True
    assert decision.draft.world.starting_location_name == "Ночной рынок Редмонда"
    feedback = model.await_args_list[-1].args[2][-1].content
    assert "technical_missing_fields" in feedback
    assert "не повод продолжать анкету" in feedback


@pytest.mark.asyncio
async def test_cli_materializes_immediately_without_second_confirmation(
    db_session: AsyncSession,
):
    campaign = await _campaign(db_session, "Immediate launch")
    response = _decision(
        "Основа есть. Начинаем со сделки на ночном рынке.",
        patch_data=MINIMAL_START_PATCH,
        finalize=True,
    )

    with (
        patch(
            "app.services.session_zero_interview.RoleModelRouter.generate_json",
            new_callable=AsyncMock,
            return_value=response,
        ),
        # Only one answer exists. Any duplicate Да/Нет confirmation would exhaust it.
        patch("builtins.input", side_effect=["Хочу Shadowrun, героя зовут Кабуто"]),
    ):
        completed = await cli.run_session_zero_interview(campaign.id, db_session)

    assert completed is True
    setup = await SessionZeroService(db_session).get(campaign.id)
    assert setup.status == "completed"
    assert setup.player_character_name == "Кабуто"
    assert setup.starting_location_name == "Ночной рынок Редмонда"


@pytest.mark.asyncio
async def test_agent_prompt_exposes_only_materialization_gaps(
    db_session: AsyncSession,
):
    campaign = await _campaign(db_session, "No optional checklist")
    interview = SessionZeroInterviewService(db_session)
    response = _decision(
        "Расскажи одним предложением, кто будет героем.",
        patch_data={"world": {"setting_name": "Shadowrun"}},
    )
    model = AsyncMock(return_value=response)

    with patch(
        "app.services.session_zero_interview.RoleModelRouter.generate_json",
        new=model,
    ):
        await interview.answer(campaign.id, "Хочу Shadowrun")

    system_prompt = model.await_args.args[2][0].content
    assert "ТЕХНИЧЕСКИЙ МИНИМУМ" in system_prompt
    assert "character.fears" not in system_prompt
    assert "character.values" not in system_prompt
    assert "world.play_style" not in system_prompt
    assert "world.starting_situation" in system_prompt
