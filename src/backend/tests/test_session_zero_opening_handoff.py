import json
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.turn_repo import TurnRepository
from app.models.campaign import CampaignCreate
from app.models.narration_validation import NarrationValidationResult, NarrationViolation
from app.runtime import install_runtime
from app.services.campaign_service import CampaignService
from app.services.session_zero_interview import SessionZeroInterviewService
from app.services.turn_authority_validator import TurnAuthorityValidator


install_runtime()


START_PATCH = {
    "world": {
        "setting_name": "Эшфорд",
        "genre": "городское мистическое расследование",
        "world_summary": "Небольшой город, где за спокойным фасадом происходят странные вещи.",
        "tone": "ночная тревога и приземлённая мистика",
        "starting_location_name": "окраина города Эшфорд",
        "starting_situation": "Посреди ночи в шатре директора появились странные гости.",
        "starting_scene_title": "Начало: окраина города Эшфорд",
        "starter_presence_confirmed": True,
        "starter_npcs": [],
    },
    "character": {
        "name": "Александр",
        "description": "Частный сыщик, привыкший разбираться в странных историях.",
        "first_goal": "Понять, что происходит в шатре директора.",
    },
}


def _decision(message: str):
    return {
        "assistant_message": message,
        "tool_calls": [
            {"name": "update_session_zero", "patch": START_PATCH},
            {"name": "finalize_session_zero"},
        ],
        "question_topics": [],
    }


def _passed() -> NarrationValidationResult:
    return NarrationValidationResult(verdict="pass", summary="ok", violations=[])


def _snapshot(turn) -> dict:
    value = turn.context_snapshot
    return json.loads(value) if isinstance(value, str) else value


async def _opening_stream(*args, **kwargs):
    yield (
        "Ночь легла на окраину Эшфорда плотным тёмным слоем. За последними домами "
        "город быстро растворяется в сырой земле, редких огнях и силуэтах временных строений. "
        "Здесь тише, чем в центре, и каждый отдельный звук кажется ближе.\n\n"
        "Шатёр директора стоит среди этой темноты как единственная точка, вокруг которой "
        "собралось всё напряжение ночи. Ткань стен едва заметно шевелится, а изнутри пробивается "
        "тусклый свет, слишком слабый, чтобы разобрать происходящее снаружи.\n\n"
        "На окраине нет обычной городской суеты. Рядом только временные строения, влажная земля "
        "и шатёр, из-за которого этой ночью возникли вопросы.\n\n"
        "За тонкой стеной шатра находятся странные гости, о которых уже известно из стартовой "
        "ситуации. Именно их появление делает этот кусок окраины важным прямо сейчас."
    )


@pytest.mark.asyncio
async def test_ready_session_zero_message_is_terminal_even_if_model_asks_question(
    db_session: AsyncSession,
):
    campaign = await CampaignService(db_session).create_campaign(
        CampaignCreate(name="Terminal handoff")
    )
    await db_session.commit()
    interview = SessionZeroInterviewService(db_session)

    with patch(
        "app.services.session_zero_interview.RoleModelRouter.generate_json",
        new_callable=AsyncMock,
        return_value=_decision("Всё готово. Ну что, начинаем?"),
    ):
        result = await interview.answer(campaign.id, "Да, запускай")

    assert result.ready_to_finalize is True
    assert result.assistant_message == (
        "Всё готово. Нулевая сессия завершена — начинаем приключение."
    )
    assert "?" not in result.assistant_message
    assert result.question_topics == []


@pytest.mark.asyncio
async def test_finalize_persists_one_system_owned_validated_opening_assistant_turn(
    db_session: AsyncSession,
):
    campaign = await CampaignService(db_session).create_campaign(
        CampaignCreate(name="Opening handoff")
    )
    await db_session.commit()
    interview = SessionZeroInterviewService(db_session)

    with patch(
        "app.services.session_zero_interview.RoleModelRouter.generate_json",
        new_callable=AsyncMock,
        return_value=_decision("Готово. Переходим к игре?"),
    ):
        decision = await interview.answer(campaign.id, "Начинаем")
    assert decision.ready_to_finalize is True

    with patch(
        "app.providers.llm_provider.LLMProvider.generate_stream",
        side_effect=_opening_stream,
    ), patch.object(
        TurnAuthorityValidator,
        "validate",
        new_callable=AsyncMock,
        return_value=_passed(),
    ):
        completion = await interview.finalize(campaign.id)
        # A browser/API retry after the successful finalize must not duplicate the opening.
        retry = await interview.finalize(campaign.id)

    assert retry.scene.id == completion.scene.id
    history = await TurnRepository(db_session).get_history(
        campaign.id,
        limit=20,
        channel="narrative",
    )
    openings = [
        turn
        for turn in history
        if turn.role == "assistant"
        and "session_zero_opening" in str(turn.context_snapshot)
    ]
    assert len(openings) == 1
    opening = openings[0]
    assert opening.parent_turn_id is None
    assert opening.scene_id == completion.scene.id
    assert opening.model_name is not None
    assert "Ночь легла на окраину Эшфорда" in opening.content
    assert len(opening.content) > 700
    assert "Что вы делаете дальше?" not in opening.content
    telemetry = _snapshot(opening)["provider_telemetry"]
    assert telemetry["opening_validation"]["status"] == "passed"
    assert telemetry["opening_raw_draft"] == opening.content


@pytest.mark.asyncio
async def test_opening_surgically_removes_player_internal_state_before_persistence(
    db_session: AsyncSession,
):
    campaign = await CampaignService(db_session).create_campaign(
        CampaignCreate(name="Opening ownership gate")
    )
    await db_session.commit()
    interview = SessionZeroInterviewService(db_session)

    with patch(
        "app.services.session_zero_interview.RoleModelRouter.generate_json",
        new_callable=AsyncMock,
        return_value=_decision("Начинаем?"),
    ):
        await interview.answer(campaign.id, "Погнали")

    bad_sentence = "Вы понимаете, что отсюда нельзя уходить."
    raw = (
        "Ночь на окраине Эшфорда холодна и тиха. Влажная земля темнеет под редкими огнями, "
        "а между временными строениями остаются узкие полосы сырой травы. "
        "Шатёр директора выделяется среди навесов плотной тёмной тканью; изнутри пробивается "
        "ровный тусклый свет. У входа закреплены обычные растяжки, и ветер едва шевелит полотно. "
        f"{bad_sentence} "
        "Странные гости уже находятся внутри шатра, и именно их появление нарушило обычный порядок. "
        "Дальше вдоль окраины видны только знакомые временные строения и редкие фонари; новых фигур "
        "или движения между ними не заметно. Слышен сухой шорох ткани и равномерный ветер, но сама "
        "стартовая ситуация остаётся сосредоточена на шатре директора."
    )
    assert len(raw) > 600

    async def raw_stream(*args, **kwargs):
        yield raw

    rejected = NarrationValidationResult(
        verdict="repair_required",
        summary="Придумана мысль героя.",
        violations=[
            NarrationViolation(
                violation_type="player_agency",
                severity="error",
                evidence=bad_sentence,
                correction="Удалить мысль героя.",
            )
        ],
    )

    with patch(
        "app.providers.llm_provider.LLMProvider.generate_stream",
        side_effect=raw_stream,
    ), patch.object(
        TurnAuthorityValidator,
        "validate",
        new_callable=AsyncMock,
        side_effect=[rejected, _passed()],
    ):
        await interview.finalize(campaign.id)

    history = await TurnRepository(db_session).get_history(
        campaign.id,
        limit=20,
        channel="narrative",
    )
    opening = next(
        turn
        for turn in history
        if turn.role == "assistant"
        and "session_zero_opening" in str(turn.context_snapshot)
    )
    assert bad_sentence not in opening.content
    assert "Странные гости уже находятся внутри шатра" in opening.content
    assert len(opening.content) > len(raw) * 0.6
    telemetry = _snapshot(opening)["provider_telemetry"]
    assert telemetry["opening_validation"]["status"] == "repaired"
    assert telemetry["opening_validation"]["repair_strategy"] == "deterministic_span_removal"
    assert telemetry["opening_raw_draft"] == raw


@pytest.mark.asyncio
async def test_opening_falls_back_to_persisted_scene_when_narrator_is_unavailable(
    db_session: AsyncSession,
):
    campaign = await CampaignService(db_session).create_campaign(
        CampaignCreate(name="Opening fallback")
    )
    await db_session.commit()
    interview = SessionZeroInterviewService(db_session)

    with patch(
        "app.services.session_zero_interview.RoleModelRouter.generate_json",
        new_callable=AsyncMock,
        return_value=_decision("Начинаем?"),
    ):
        await interview.answer(campaign.id, "Погнали")

    async def broken_stream(*args, **kwargs):
        from app.providers.llm_provider import LLMProviderError
        raise LLMProviderError("offline")
        yield ""  # pragma: no cover

    with patch(
        "app.providers.llm_provider.LLMProvider.generate_stream",
        side_effect=broken_stream,
    ):
        completion = await interview.finalize(campaign.id)

    history = await TurnRepository(db_session).get_history(
        campaign.id,
        limit=20,
        channel="narrative",
    )
    opening = next(
        turn
        for turn in history
        if turn.role == "assistant"
        and "session_zero_opening" in str(turn.context_snapshot)
    )
    assert opening.scene_id == completion.scene.id
    assert "окраина города Эшфорд" in opening.content
    assert "Посреди ночи в шатре директора появились странные гости" in opening.content
    assert "Начинаем" not in opening.content
