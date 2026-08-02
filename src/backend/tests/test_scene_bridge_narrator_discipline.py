from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.location_repo import LocationRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.scene_bridge_table import SceneBridge
from app.models.campaign import CampaignCreate, CampaignUpdate
from app.models.character import CharacterCreate
from app.models.location import LocationCreate
from app.models.scene import SceneCreate
from app.models.scene_state import LocationExitCreate, SceneStateUpdate
from app.models.turn import ChatMessage
from app.services.context_compiler import ContextCompiler
from app.services.scene_bridge_service import SceneBridgeService
from app.services.scene_lifecycle import SceneLifecycleService
from app.services.scene_state_service import SceneStateService
from app.services.scene_transition_executor import SceneTransitionExecutor
from app.services.turn_planner import (
    NarrationPolicy,
    SceneTransitionPlan,
    TurnPlan,
    TurnPlanner,
)


async def _tavern_state(db_session: AsyncSession):
    campaign_id = uuid4()
    campaigns = CampaignRepository(db_session)
    locations = LocationRepository(db_session)
    entities = EntityRepository(db_session)
    scenes = SceneRepository(db_session)
    state = SceneStateService(db_session)

    await campaigns.create(campaign_id, CampaignCreate(name="Scene bridge"))
    tavern = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Таверна"),
    )
    hall = await locations.create(
        campaign_id,
        LocationCreate(
            canonical_name="Общий зал",
            parent_location_id=tavern.id,
        ),
    )
    room = await locations.create(
        campaign_id,
        LocationCreate(
            canonical_name="Комната №3",
            parent_location_id=tavern.id,
        ),
    )
    hero = await entities.create_character(
        campaign_id,
        CharacterCreate(canonical_name="Эйдан", current_location_id=hall.id),
    )
    bartender = await entities.create_character(
        campaign_id,
        CharacterCreate(canonical_name="Бармен", current_location_id=hall.id),
    )
    await campaigns.update(
        campaign_id,
        CampaignUpdate(player_character_id=hero.id),
    )
    source = await scenes.create(
        campaign_id,
        SceneCreate(title="Вечер в общем зале", location_id=hall.id),
    )
    await scenes.add_participant(source.id, bartender.id)
    await SceneLifecycleService(db_session).activate(campaign_id, source.id)
    await state.update(
        campaign_id,
        source.id,
        SceneStateUpdate(
            world_time_label="поздний вечер",
            world_time_order=5,
            scene_goal="утром найти Купцов",
            active_conflict="долг за ночлег ещё не обсуждён",
        ),
    )
    await state.create_exit(
        campaign_id,
        hall.id,
        LocationExitCreate(
            to_location_id=room.id,
            label="Лестница к комнатам",
            bidirectional=True,
            reverse_label="Лестница в общий зал",
        ),
    )
    return campaign_id, source, room, hero, bartender


@pytest.mark.asyncio
async def test_transition_builds_compact_bridge_and_leaves_bartender_behind(
    db_session: AsyncSession,
):
    campaign_id, source, room, hero, bartender = await _tavern_state(db_session)
    executor = SceneTransitionExecutor(db_session)

    applied = await executor.apply(
        campaign_id,
        source.id,
        None,
        SceneTransitionPlan(
            required=True,
            transition_type="location_transition",
            destination_location="Комната №3",
            destination_parent_location="Таверна",
            scene_title="Ночь в комнате",
            reason="игрок снял комнату и ушёл из общего зала",
            bridge_summary="Комната оплачена; разговор с барменом завершён.",
            carryover_goals=["утром найти Купцов"],
            unresolved_threads=["уточнить правила доступа к Купцам"],
        ),
    )
    assert applied is not None
    bridge = await SceneBridgeService(db_session).get_for_target_scene(
        campaign_id,
        applied.target_scene_id,
    )
    assert bridge is not None
    assert bridge.previous_scene_summary == (
        "Комната оплачена; разговор с барменом завершён."
    )
    assert bridge.carried_participant_ids == [hero.id]
    assert bridge.departed_participant_ids == [bartender.id]
    assert bridge.departed_participant_names == ["Бармен"]
    assert "утром найти Купцов" in bridge.carried_goals
    assert "уточнить правила доступа к Купцам" in bridge.unresolved_threads
    assert any("Бармен remained" in fact for fact in bridge.negative_placement_facts)

    messages, metadata = await ContextCompiler(db_session).compile_context(
        campaign_id=campaign_id,
        scene_id=applied.target_scene_id,
        current_user_content="Я закрываю дверь.",
    )
    system = messages[0].content
    assert "[SCENE BRIDGE]" in system
    assert "Комната оплачена" in system
    assert "Бармен remained" in system
    assert "Do not import its full cast" in system
    assert metadata["scene_bridge"]["target_scene_id"] == str(
        applied.target_scene_id
    )
    target_state = await SceneStateService(db_session).get(
        campaign_id,
        applied.target_scene_id,
    )
    assert target_state.location_id == room.id
    assert target_state.participant_ids == [hero.id]

    assert await executor.mark_applied(applied.transition_id)
    row = (
        await db_session.execute(
            select(SceneBridge).where(
                SceneBridge.transition_id == str(applied.transition_id)
            )
        )
    ).scalar_one()
    assert row.status == "applied"


@pytest.mark.asyncio
async def test_failed_transition_rolls_back_bridge_and_removes_it_from_context(
    db_session: AsyncSession,
):
    campaign_id, source, _, _, _ = await _tavern_state(db_session)
    executor = SceneTransitionExecutor(db_session)
    applied = await executor.apply(
        campaign_id,
        source.id,
        None,
        SceneTransitionPlan(
            required=True,
            transition_type="location_transition",
            destination_location="Комната №3",
            destination_parent_location="Таверна",
            reason="игрок уходит спать",
        ),
    )
    assert applied is not None
    assert await executor.rollback_transition(applied.transition_id)

    row = (
        await db_session.execute(
            select(SceneBridge).where(
                SceneBridge.transition_id == str(applied.transition_id)
            )
        )
    ).scalar_one()
    assert row.status == "rolled_back"
    assert await SceneBridgeService(db_session).get_for_target_scene(
        campaign_id,
        applied.target_scene_id,
    ) is None



def test_new_complication_requires_an_established_source():
    with pytest.raises(ValidationError):
        NarrationPolicy(
            dramatic_mode="tense",
            allow_new_complication=True,
        )


def test_narrator_contract_protects_agency_and_allows_calm_endings():
    plan = TurnPlan(
        player_intent="Осмотреть тихую комнату.",
        resolution="observation",
        narration_policy=NarrationPolicy(
            dramatic_mode="calm",
            allow_new_complication=False,
            pending_player_choice="Решить, ложиться ли спать.",
            protected_player_decisions=[
                "решение лечь спать",
                "эмоциональная оценка комнаты",
            ],
        ),
        ending_hook="Комната осмотрена; дальнейшее действие выбирает игрок.",
    )
    result = TurnPlanner.inject_plan(
        [
            ChatMessage(role="system", content="Campaign truth"),
            ChatMessage(role="user", content="Я осматриваю комнату."),
        ],
        plan,
    )
    contract = result[0].content
    assert "Player agency is a hard boundary" in contract
    assert "Calm and routine scenes may remain calm" in contract
    assert '"allow_new_complication": false' in contract
    assert "решение лечь спать" in contract
    assert "Never write the protagonist's unprovided dialogue" in contract
