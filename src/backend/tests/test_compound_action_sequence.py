from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.action_sequence_table import ActionSequence, ActionStep
from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.location_repo import LocationRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.repositories.turn_repo import TurnRepository
from app.db.scene_transition_table import SceneTransition
from app.models.campaign import CampaignCreate, CampaignUpdate
from app.models.character import CharacterCreate
from app.models.location import LocationCreate
from app.models.scene import SceneCreate
from app.models.scene_state import LocationExitCreate, SceneStateUpdate
from app.models.turn import ChatMessage, TurnCreate
from app.services.action_sequence_executor import ActionSequenceExecutor
from app.services.scene_lifecycle import SceneLifecycleService
from app.services.scene_state_service import SceneStateService
from app.services.scene_transition_executor import SceneTransitionExecutor
from app.services.turn_planner import (
    ActionSequencePlan,
    ActionStepPlan,
    SceneTransitionPlan,
    TurnPlan,
    TurnPlanner,
)


def _compound_plan() -> TurnPlan:
    return TurnPlan(
        player_intent=(
            "Снять комнату, лечь спать, а утром отправиться к Купцам."
        ),
        resolution="sequence",
        action_sequence=ActionSequencePlan(
            summary="Спокойно завершить вечер и утром прибыть к Купцам.",
            steps=[
                ActionStepPlan(
                    action_type="service",
                    intent="Оплатить обычную гостевую комнату.",
                    resolution="auto_success",
                    safe_mundane=True,
                    observable_outcome="Комната оплачена и ключ получен.",
                ),
                ActionStepPlan(
                    action_type="movement",
                    intent="Подняться в гостевую комнату.",
                    resolution="auto_success",
                    safe_mundane=True,
                    observable_outcome="Герой оказывается один в комнате.",
                    transition=SceneTransitionPlan(
                        required=True,
                        transition_type="location_transition",
                        destination_location="Гостевая комната №3",
                        destination_parent_location="Таверна",
                        scene_title="Ночь в гостевой комнате",
                        reason="Игрок идёт в оплаченную комнату.",
                    ),
                ),
                ActionStepPlan(
                    action_type="rest",
                    intent="Спать до утра.",
                    resolution="auto_success",
                    safe_mundane=True,
                    observable_outcome="Герой выспался; наступило утро.",
                    transition=SceneTransitionPlan(
                        required=True,
                        transition_type="time_transition",
                        elapsed_time="8 часов",
                        time_after="утро",
                        scene_title="Утро в гостевой комнате",
                        reason="Безопасный сон до утра.",
                    ),
                ),
                ActionStepPlan(
                    action_type="movement",
                    intent="Отправиться ко входу Купцов.",
                    resolution="auto_success",
                    safe_mundane=True,
                    observable_outcome="Герой прибывает ко входу Купцов.",
                    transition=SceneTransitionPlan(
                        required=True,
                        transition_type="location_transition",
                        destination_location="Служебный вход Купцов",
                        destination_parent_location="Рыночный квартал",
                        scene_title="Утро у входа Купцов",
                        reason="Игрок следует по известному спокойному маршруту.",
                    ),
                ),
            ],
        ),
        observable_consequences=["Вечер и ночь проходят без происшествий."],
        canon_constraints=["Бармен остаётся в общем зале."],
        narration_guidance=["Кратко связать выполненные шаги."],
        ending_hook="Герой стоит у входа Купцов утром.",
    )


async def _world(db_session: AsyncSession):
    campaign_id = uuid4()
    campaigns = CampaignRepository(db_session)
    locations = LocationRepository(db_session)
    entities = EntityRepository(db_session)
    scenes = SceneRepository(db_session)

    await campaigns.create(campaign_id, CampaignCreate(name="Compound actions"))
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
            canonical_name="Гостевая комната №3",
            parent_location_id=tavern.id,
        ),
    )
    market = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Рыночный квартал"),
    )
    merchants = await locations.create(
        campaign_id,
        LocationCreate(
            canonical_name="Служебный вход Купцов",
            parent_location_id=market.id,
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
    state = SceneStateService(db_session)
    await state.update(
        campaign_id,
        source.id,
        SceneStateUpdate(world_time_label="поздний вечер", world_time_order=10),
    )
    await state.create_exit(
        campaign_id,
        hall.id,
        LocationExitCreate(to_location_id=room.id, label="Лестница к комнатам"),
    )
    await state.create_exit(
        campaign_id,
        room.id,
        LocationExitCreate(to_location_id=merchants.id, label="Дорога к Купцам"),
    )
    turn = await TurnRepository(db_session).create(
        campaign_id,
        TurnCreate(
            role="user",
            content="Снимаю комнату, сплю и утром иду к Купцам.",
            scene_id=source.id,
        ),
    )
    await db_session.flush()
    return {
        "campaign_id": campaign_id,
        "source": source,
        "hall": hall,
        "room": room,
        "merchants": merchants,
        "hero": hero,
        "bartender": bartender,
        "turn": turn,
    }


@pytest.mark.asyncio
async def test_compound_sequence_executes_every_safe_step_in_order(
    db_session: AsyncSession,
):
    world = await _world(db_session)
    plan = _compound_plan()
    applied = await SceneTransitionExecutor(db_session).apply(
        world["campaign_id"],
        world["source"].id,
        world["turn"].id,
        plan.scene_transition,
    )

    assert applied is not None
    assert applied.action_sequence is not None
    execution = applied.action_sequence
    assert execution.completed_steps == 4
    assert execution.blocked_step_index is None
    assert [step.status for step in execution.steps] == ["completed"] * 4
    assert applied.scene.location_id == world["merchants"].id

    final_state = await SceneStateService(db_session).get(
        world["campaign_id"],
        applied.target_scene_id,
    )
    assert final_state.world_time_label == "утро"
    assert final_state.world_time_order == 13
    assert final_state.participant_ids == [world["hero"].id]

    transitions = (
        await db_session.execute(
            select(SceneTransition).order_by(SceneTransition.created_at)
        )
    ).scalars().all()
    assert len(transitions) == 4
    assert transitions[-1].transition_type == "action_sequence"
    assert transitions[-1].detector == "compound_action_executor"

    assert await SceneTransitionExecutor(db_session).mark_applied(
        applied.transition_id
    )
    assert all(row.status == "applied" for row in transitions)


@pytest.mark.asyncio
async def test_sequence_stops_at_real_obstacle_and_skips_later_steps(
    db_session: AsyncSession,
):
    world = await _world(db_session)
    cellar = await LocationRepository(db_session).create(
        world["campaign_id"],
        LocationCreate(canonical_name="Закрытый подвал"),
    )
    plan = TurnPlan(
        player_intent="Плачу за комнату, иду в подвал и сплю там.",
        resolution="sequence",
        action_sequence=ActionSequencePlan(
            steps=[
                ActionStepPlan(
                    action_type="service",
                    intent="Оплатить комнату.",
                    resolution="auto_success",
                    safe_mundane=True,
                    observable_outcome="Комната оплачена.",
                ),
                ActionStepPlan(
                    action_type="movement",
                    intent="Пройти в закрытый подвал.",
                    resolution="auto_success",
                    safe_mundane=False,
                    observable_outcome="Герой оказывается в подвале.",
                    transition=SceneTransitionPlan(
                        required=True,
                        transition_type="location_transition",
                        destination_location=cellar.canonical_name,
                    ),
                ),
                ActionStepPlan(
                    action_type="rest",
                    intent="Лечь спать в подвале.",
                    resolution="auto_success",
                    safe_mundane=True,
                    observable_outcome="Наступает утро.",
                    transition=SceneTransitionPlan(
                        required=True,
                        transition_type="time_transition",
                        time_after="утро",
                    ),
                ),
            ]
        ),
    )

    applied = await SceneTransitionExecutor(db_session).apply(
        world["campaign_id"],
        world["source"].id,
        world["turn"].id,
        plan.scene_transition,
    )
    execution = applied.action_sequence
    assert execution is not None
    assert execution.completed_steps == 1
    assert execution.blocked_step_index == 1
    assert [step.status for step in execution.steps] == [
        "completed",
        "blocked",
        "skipped",
    ]
    assert execution.final_scene_id == world["source"].id
    assert "not an available exit" in execution.steps[1].blocking_reason


@pytest.mark.asyncio
async def test_failed_sequence_rolls_back_all_intermediate_scenes(
    db_session: AsyncSession,
):
    world = await _world(db_session)
    plan = _compound_plan()
    transition_executor = SceneTransitionExecutor(db_session)
    applied = await transition_executor.apply(
        world["campaign_id"],
        world["source"].id,
        world["turn"].id,
        plan.scene_transition,
    )
    assert await transition_executor.rollback_transition(applied.transition_id)

    campaign = await CampaignRepository(db_session).get_by_id(world["campaign_id"])
    assert campaign.current_scene_id == world["source"].id
    hero = await EntityRepository(db_session).get_character(world["hero"].id)
    assert hero.current_location_id == world["hall"].id

    sequence = await db_session.get(
        ActionSequence,
        str(applied.action_sequence.sequence_id),
    )
    assert sequence.status == "rolled_back"
    steps = (
        await db_session.execute(
            select(ActionStep)
            .where(ActionStep.sequence_id == sequence.id)
            .order_by(ActionStep.step_index)
        )
    ).scalars().all()
    assert all(step.status == "rolled_back" for step in steps)

    transitions = (
        await db_session.execute(select(SceneTransition))
    ).scalars().all()
    assert all(row.status == "rolled_back" for row in transitions)


@pytest.mark.asyncio
async def test_execution_contract_is_injected_and_reused(
    db_session: AsyncSession,
):
    world = await _world(db_session)
    plan = _compound_plan()
    transition_executor = SceneTransitionExecutor(db_session)
    applied = await transition_executor.apply(
        world["campaign_id"],
        world["source"].id,
        world["turn"].id,
        plan.scene_transition,
    )

    messages = TurnPlanner.inject_plan(
        [ChatMessage(role="system", content="Campaign truth")],
        plan,
    )
    assert "[EXECUTED ACTION SEQUENCE]" in messages[0].content
    assert "4. Отправиться ко входу Купцов. -> COMPLETED" in messages[0].content
    assert "Do not reopen or interrupt" in messages[0].content

    await transition_executor.mark_applied(applied.transition_id)
    reused = await transition_executor.existing_for_turn(
        world["campaign_id"],
        world["turn"].id,
    )
    assert reused.action_sequence.status == "applied"
    regenerated = TurnPlanner.inject_plan(
        [ChatMessage(role="system", content="Campaign truth")],
        _compound_plan(),
    )
    assert "[EXECUTED ACTION SEQUENCE]" in regenerated[0].content
    assert "Sequence status: applied" in regenerated[0].content


def test_full_turn_and_undo_keep_sequence_atomic(
    client: TestClient,
    db_session: AsyncSession,
):
    campaign_id = client.post(
        "/api/campaigns",
        json={"name": "Atomic compound turn"},
    ).json()["id"]
    hall = client.post(
        f"/api/campaigns/{campaign_id}/locations",
        json={"canonical_name": "Общий зал"},
    ).json()
    room = client.post(
        f"/api/campaigns/{campaign_id}/locations",
        json={"canonical_name": "Гостевая комната №3"},
    ).json()
    merchants = client.post(
        f"/api/campaigns/{campaign_id}/locations",
        json={"canonical_name": "Служебный вход Купцов"},
    ).json()
    hero = client.post(
        f"/api/campaigns/{campaign_id}/characters",
        json={"canonical_name": "Эйдан", "current_location_id": hall["id"]},
    ).json()
    client.put(
        f"/api/campaigns/{campaign_id}",
        json={"player_character_id": hero["id"]},
    )
    source = client.post(
        f"/api/campaigns/{campaign_id}/scenes",
        json={"title": "Вечер", "location_id": hall["id"]},
    ).json()
    client.post(
        f"/api/campaigns/{campaign_id}/locations/{hall['id']}/exits",
        json={"to_location_id": room["id"], "label": "Лестница"},
    )
    client.post(
        f"/api/campaigns/{campaign_id}/locations/{room['id']}/exits",
        json={"to_location_id": merchants["id"], "label": "Дорога к Купцам"},
    )

    captured = {}

    async def narrator(messages, *args, **kwargs):
        captured["system"] = messages[0].content
        yield "Ты снимаешь комнату, спокойно спишь и утром приходишь к Купцам."

    with patch(
        "app.services.turn_planner.TurnPlanner.plan",
        new_callable=AsyncMock,
        return_value=_compound_plan(),
    ), patch(
        "app.providers.llm_provider.LLMProvider.generate_stream",
        side_effect=narrator,
    ):
        response = client.post(
            f"/api/campaigns/{campaign_id}/turns",
            json={
                "role": "user",
                "content": "Снимаю комнату, сплю и утром иду к Купцам.",
                "scene_id": source["id"],
            },
        )

    assert response.status_code == 200, response.text
    assert "[EXECUTED ACTION SEQUENCE]" in captured["system"]
    snapshot = client.get(f"/api/campaigns/{campaign_id}/debugger").json()
    assert snapshot["campaign"]["player_location_id"] == merchants["id"]

    sequence = (
        db_session.execute(
            select(ActionSequence).where(
                ActionSequence.campaign_id == campaign_id
            )
        )
    )
    # The TestClient runs the async dependency in its own loop; inspect through API
    # effects here and use the undo endpoint as the durable contract.
    undone = client.post(f"/api/campaigns/{campaign_id}/turns/undo")
    assert undone.status_code == 200, undone.text
    snapshot = client.get(f"/api/campaigns/{campaign_id}/debugger").json()
    assert snapshot["active_scene"]["id"] == source["id"]
    assert snapshot["campaign"]["player_location_id"] == hall["id"]
