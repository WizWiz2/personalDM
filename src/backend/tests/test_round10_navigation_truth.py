from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.location_repo import LocationRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.repositories.turn_repo import TurnRepository
from app.models.campaign import CampaignCreate, CampaignUpdate
from app.models.character import CharacterCreate
from app.models.location import LocationCreate
from app.models.scene import SceneCreate
from app.models.scene_state import LocationExitCreate
from app.models.turn import TurnCreate
from app.models.turn_authority import TurnAuthority
from app.services.scene_lifecycle import SceneLifecycleService
from app.services.scene_state_service import SceneStateService
from app.services.scene_transition_executor import SceneTransitionExecutor
from app.services.turn_planner import (
    ActionSequencePlan,
    ActionStepPlan,
    SceneTransitionPlan,
)


async def _navigation_world(db_session: AsyncSession):
    campaign_id = uuid4()
    campaigns = CampaignRepository(db_session)
    entities = EntityRepository(db_session)
    locations = LocationRepository(db_session)
    scenes = SceneRepository(db_session)

    await campaigns.create(campaign_id, CampaignCreate(name="Round 10 navigation"))
    office = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Офис детектива"),
    )
    street = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Улица Нижнего Города"),
    )
    department = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Департамент Киберпреступлений"),
    )
    hero = await entities.create_character(
        campaign_id,
        CharacterCreate(canonical_name="Рэт", current_location_id=office.id),
    )
    await campaigns.update(
        campaign_id,
        CampaignUpdate(player_character_id=hero.id),
    )
    source = await scenes.create(
        campaign_id,
        SceneCreate(title="Офис детектива", location_id=office.id),
    )
    await SceneLifecycleService(db_session).activate(campaign_id, source.id)
    await SceneStateService(db_session).create_exit(
        campaign_id,
        office.id,
        LocationExitCreate(to_location_id=street.id, label="Выход на улицу"),
    )
    await db_session.commit()
    return campaign_id, hero, office, street, department, source


@pytest.mark.asyncio
async def test_explicit_player_travel_discovers_route_to_existing_location(
    db_session: AsyncSession,
):
    campaign_id, hero, office, _, department, source = await _navigation_world(db_session)
    user_turn = await TurnRepository(db_session).create(
        campaign_id,
        TurnCreate(role="user", content="Еду в Департамент Киберпреступлений."),
    )

    applied = await SceneTransitionExecutor(db_session).apply(
        campaign_id,
        source.id,
        user_turn.id,
        SceneTransitionPlan(
            required=True,
            transition_type="location_transition",
            destination_location=department.canonical_name,
            reason="Игрок явно едет в известное городское место.",
        ),
    )

    assert applied is not None
    assert applied.source_location_id == office.id
    assert applied.target_location_id == department.id
    assert applied.target_scene_id != source.id
    character = await EntityRepository(db_session).get_character(hero.id)
    assert character is not None
    assert character.current_location_id == department.id

    office_exits = await SceneStateService(db_session).list_exits(
        campaign_id,
        office.id,
        include_hidden=True,
    )
    department_exits = await SceneStateService(db_session).list_exits(
        campaign_id,
        department.id,
        include_hidden=True,
    )
    assert any(item.to_location_id == department.id for item in office_exits)
    assert any(item.to_location_id == office.id for item in department_exits)


@pytest.mark.asyncio
async def test_explicit_travel_does_not_reactivate_inactive_direct_route(
    db_session: AsyncSession,
):
    campaign_id, _, office, _, department, source = await _navigation_world(db_session)
    state = SceneStateService(db_session)
    await state.create_exit(
        campaign_id,
        office.id,
        LocationExitCreate(
            to_location_id=department.id,
            label="Закрытый служебный тоннель",
            access_rule="проход перекрыт полицией",
            active=False,
        ),
    )
    user_turn = await TurnRepository(db_session).create(
        campaign_id,
        TurnCreate(role="user", content="Еду в Департамент Киберпреступлений."),
    )

    with pytest.raises(ValueError, match="route is currently inactive"):
        await SceneTransitionExecutor(db_session).apply(
            campaign_id,
            source.id,
            user_turn.id,
            SceneTransitionPlan(
                required=True,
                transition_type="location_transition",
                destination_location=department.canonical_name,
            ),
        )

    direct = next(
        item
        for item in await state.list_exits(campaign_id, office.id, include_hidden=True)
        if item.to_location_id == department.id
    )
    assert direct.active is False
    assert direct.access_rule == "проход перекрыт полицией"


@pytest.mark.asyncio
async def test_compound_explicit_travel_propagates_discovery_to_movement_step(
    db_session: AsyncSession,
):
    campaign_id, hero, _, _, department, source = await _navigation_world(db_session)
    user_turn = await TurnRepository(db_session).create(
        campaign_id,
        TurnCreate(
            role="user",
            content="Еду в Департамент, вхожу в вестибюль и осматриваюсь.",
        ),
    )
    sequence = ActionSequencePlan(
        summary="Доехать до Департамента и осмотреть вестибюль.",
        steps=[
            ActionStepPlan(
                action_type="movement",
                intent="Доехать до Департамента",
                resolution="auto_success",
                safe_mundane=True,
                observable_outcome="Рэт прибывает в Департамент Киберпреступлений.",
                transition=SceneTransitionPlan(
                    required=True,
                    transition_type="location_transition",
                    destination_location=department.canonical_name,
                ),
            ),
            ActionStepPlan(
                action_type="observation",
                intent="Осмотреть вестибюль",
                resolution="auto_success",
                safe_mundane=True,
                observable_outcome="Рэт осматривает вестибюль.",
            ),
        ],
    )
    boundary = SceneTransitionPlan(
        required=True,
        transition_type="focus_transition",
        reason="Execute ordered player action sequence.",
        sequence_payload=sequence.model_dump(mode="json"),
    )

    applied = await SceneTransitionExecutor(db_session).apply(
        campaign_id,
        source.id,
        user_turn.id,
        boundary,
    )

    assert applied is not None
    assert applied.action_sequence is not None
    assert applied.action_sequence.blocked_step_index is None
    assert [step.status for step in applied.action_sequence.steps] == [
        "completed",
        "completed",
    ]
    assert applied.target_location_id == department.id
    character = await EntityRepository(db_session).get_character(hero.id)
    assert character is not None
    assert character.current_location_id == department.id


@pytest.mark.asyncio
async def test_generic_travel_shorthand_cannot_choose_between_two_departments(
    db_session: AsyncSession,
):
    campaign_id, _, _, _, department, source = await _navigation_world(db_session)
    await LocationRepository(db_session).create(
        campaign_id,
        LocationCreate(canonical_name="Департамент Транспорта"),
    )
    user_turn = await TurnRepository(db_session).create(
        campaign_id,
        TurnCreate(role="user", content="Еду в Департамент."),
    )

    with pytest.raises(ValueError, match="destination reference is ambiguous"):
        await SceneTransitionExecutor(db_session).apply(
            campaign_id,
            source.id,
            user_turn.id,
            SceneTransitionPlan(
                required=True,
                transition_type="location_transition",
                destination_location=department.canonical_name,
            ),
        )


def test_blocked_execution_removes_planned_success_from_authority():
    authority = TurnAuthority(
        campaign_id=uuid4(),
        trigger_turn_id=uuid4(),
        player_character_name="Рэт",
        player_input="Еду в Департамент и говорю с дежурным.",
        scene_disposition="sequence",
        transition_type="action_sequence",
        source_location_path=["Офис детектива"],
        target_location_path=["Офис детектива"],
        observable_consequences=[
            "Рэт прибывает в Департамент.",
            "Дежурный отвечает на вопрос.",
        ],
        action_sequence={
            "status": "prepared",
            "planned_steps": 2,
            "completed_steps": 0,
            "blocked_step_index": 0,
            "steps": [
                {
                    "step_index": 0,
                    "intent": "Доехать до Департамента",
                    "status": "blocked",
                    "blocking_reason": "Destination route is currently inactive",
                },
                {
                    "step_index": 1,
                    "intent": "Поговорить с дежурным",
                    "status": "skipped",
                },
            ],
        },
    )

    assert authority.observable_consequences == ["Путь туда сейчас недоступен."]
    payload = authority.narrator_payload()
    assert "Рэт прибывает в Департамент." not in payload["observable_consequences"]
    assert "Дежурный отвечает на вопрос." not in payload["observable_consequences"]
    assert "Destination route is currently inactive" not in payload["observable_consequences"]
    assert "технических статусов движка" in payload["narration_guidance"][-1]


@pytest.mark.asyncio
async def test_non_player_transition_keeps_strict_existing_exit_contract(
    db_session: AsyncSession,
):
    campaign_id, _, _, _, department, source = await _navigation_world(db_session)

    with pytest.raises(ValueError, match="not an available exit"):
        await SceneTransitionExecutor(db_session).apply(
            campaign_id,
            source.id,
            None,
            SceneTransitionPlan(
                required=True,
                transition_type="location_transition",
                destination_location=department.canonical_name,
                reason="Нет прямого разрешения игрока на открытие маршрута.",
            ),
        )
