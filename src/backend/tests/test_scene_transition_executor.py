from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.location_repo import LocationRepository
from app.db.repositories.scene_repo import SceneRepository
from app.models.campaign import CampaignCreate, CampaignUpdate
from app.models.character import CharacterCreate
from app.models.location import LocationCreate
from app.models.scene import SceneCreate
from app.services.scene_lifecycle import SceneLifecycleService
from app.services.scene_transition_executor import SceneTransitionExecutor
from app.services.turn_planner import SceneTransitionPlan


@pytest.mark.asyncio
async def test_executor_creates_private_scene_without_inheriting_bartender(
    db_session: AsyncSession,
):
    campaign_id = uuid4()
    campaigns = CampaignRepository(db_session)
    entities = EntityRepository(db_session)
    locations = LocationRepository(db_session)
    scenes = SceneRepository(db_session)

    await campaigns.create(
        campaign_id,
        CampaignCreate(name="Executor isolation"),
    )
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
        CharacterCreate(canonical_name="Эйдан"),
    )
    bartender = await entities.create_character(
        campaign_id,
        CharacterCreate(canonical_name="Бармен"),
    )
    await campaigns.update(
        campaign_id,
        CampaignUpdate(player_character_id=hero.id),
    )
    source = await scenes.create(
        campaign_id,
        SceneCreate(title="Общий зал", location_id=hall.id),
    )
    await scenes.add_participant(source.id, hero.id)
    await scenes.add_participant(source.id, bartender.id)
    await SceneLifecycleService(db_session).activate(campaign_id, source.id)

    result = await SceneTransitionExecutor(db_session).apply(
        campaign_id,
        source.id,
        None,
        SceneTransitionPlan(
            required=True,
            transition_type="location_transition",
            destination_location="Комната №3",
            destination_parent_location="Таверна",
            scene_title="Ночь в комнате",
            carry_participants=[],
            reason="Игрок ушёл из общего зала.",
        ),
    )

    assert result is not None
    assert result.target_location_id == room.id
    assert result.target_scene_id != source.id
    target = await scenes.get_by_id(result.target_scene_id)
    assert target is not None
    assert target.participants == [hero.id]


@pytest.mark.asyncio
async def test_route_labeled_current_location_rewrites_to_unique_return_exit(
    db_session: AsyncSession,
):
    from app.models.scene_state import LocationExitCreate
    from app.services.scene_state_service import SceneStateService

    campaign_id = uuid4()
    campaigns = CampaignRepository(db_session)
    entities = EntityRepository(db_session)
    locations = LocationRepository(db_session)
    scenes = SceneRepository(db_session)
    state = SceneStateService(db_session)

    await campaigns.create(campaign_id, CampaignCreate(name="Return rewrite"))
    tavern = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Трактир «Якорь»"),
    )
    street = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="наружу -> Окрестности — Трактир «Якорь»"),
    )
    hero = await entities.create_character(
        campaign_id,
        CharacterCreate(canonical_name="Вера", current_location_id=street.id),
    )
    await campaigns.update(campaign_id, CampaignUpdate(player_character_id=hero.id))
    source = await scenes.create(
        campaign_id,
        SceneCreate(title="наружу -> Окрестности — Трактир «Якорь»", location_id=street.id),
    )
    await scenes.add_participant(source.id, hero.id, allow_movement=True)
    await SceneLifecycleService(db_session).activate(campaign_id, source.id)
    await state.create_exit(
        campaign_id,
        street.id,
        LocationExitCreate(to_location_id=tavern.id, label="Трактир «Якорь»"),
    )

    resolved = await SceneTransitionExecutor(db_session)._resolve_existing_location(
        campaign_id,
        street.id,
        "наружу -> Окрестности — Трактир «Якорь»",
    )

    assert resolved is not None
    assert resolved.id == tavern.id


@pytest.mark.asyncio
async def test_executor_does_not_persist_arrow_path_as_canonical_name(
    db_session: AsyncSession,
):
    campaign_id = uuid4()
    campaigns = CampaignRepository(db_session)
    entities = EntityRepository(db_session)
    locations = LocationRepository(db_session)
    scenes = SceneRepository(db_session)

    await campaigns.create(campaign_id, CampaignCreate(name="No arrow names"))
    tavern = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Трактир «Якорь»"),
    )
    hero = await entities.create_character(
        campaign_id,
        CharacterCreate(canonical_name="Вера", current_location_id=tavern.id),
    )
    await campaigns.update(campaign_id, CampaignUpdate(player_character_id=hero.id))
    source = await scenes.create(
        campaign_id,
        SceneCreate(title="Утро в трактире", location_id=tavern.id),
    )
    await scenes.add_participant(source.id, hero.id, allow_movement=True)
    await SceneLifecycleService(db_session).activate(campaign_id, source.id)

    result = await SceneTransitionExecutor(db_session).apply(
        campaign_id,
        source.id,
        None,
        SceneTransitionPlan(
            required=True,
            transition_type="location_transition",
            destination_location="наружу -> Окрестности — Трактир «Якорь»",
            scene_title="наружу -> Окрестности — Трактир «Якорь»",
            carry_participants=["Вера"],
            reason="Игрок выходит наружу.",
        ),
        allow_route_discovery=True,
    )

    assert result is not None
    created = await locations.get_by_id(result.target_location_id)
    assert created is not None
    assert "->" not in created.canonical_name
    assert created.canonical_name == "Окрестности — Трактир «Якорь»"
    target = await scenes.get_by_id(result.target_scene_id)
    assert target is not None
    assert "->" not in target.title


@pytest.mark.asyncio
async def test_activate_removes_player_from_previous_scene(db_session: AsyncSession):
    from app.db.tables import SceneParticipant
    from sqlalchemy import select

    campaign_id = uuid4()
    campaigns = CampaignRepository(db_session)
    entities = EntityRepository(db_session)
    locations = LocationRepository(db_session)
    scenes = SceneRepository(db_session)

    await campaigns.create(campaign_id, CampaignCreate(name="Player occupancy"))
    tavern = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Трактир"),
    )
    street = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Улица"),
    )
    hero = await entities.create_character(
        campaign_id,
        CharacterCreate(canonical_name="Вера", current_location_id=tavern.id),
    )
    await campaigns.update(campaign_id, CampaignUpdate(player_character_id=hero.id))
    first = await scenes.create(
        campaign_id,
        SceneCreate(title="Трактир", location_id=tavern.id),
    )
    await scenes.add_participant(first.id, hero.id, allow_movement=True)
    await SceneLifecycleService(db_session).activate(campaign_id, first.id)
    second = await scenes.create(
        campaign_id,
        SceneCreate(title="Улица", location_id=street.id),
    )
    await SceneLifecycleService(db_session).activate(campaign_id, second.id)

    leftover = (
        await db_session.execute(
            select(SceneParticipant).where(
                SceneParticipant.scene_id == str(first.id),
                SceneParticipant.entity_id == str(hero.id),
            )
        )
    ).scalar_one_or_none()
    current = (
        await db_session.execute(
            select(SceneParticipant).where(
                SceneParticipant.scene_id == str(second.id),
                SceneParticipant.entity_id == str(hero.id),
            )
        )
    ).scalar_one_or_none()

    assert leftover is None
    assert current is not None


@pytest.mark.asyncio
async def test_exit_travel_authorizes_unique_available_exit(db_session: AsyncSession):
    from app.db.repositories.turn_repo import TurnRepository
    from app.models.scene_state import LocationExitCreate
    from app.models.turn import TurnCreate
    from app.services.player_destination_authorization import PlayerDestinationAuthorizer
    from app.services.scene_state_service import SceneStateService

    campaign_id = uuid4()
    campaigns = CampaignRepository(db_session)
    entities = EntityRepository(db_session)
    locations = LocationRepository(db_session)
    scenes = SceneRepository(db_session)

    await campaigns.create(campaign_id, CampaignCreate(name="Unique exit"))
    tavern = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Трактир «Якорь»"),
    )
    street = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Окрестности — Трактир «Якорь»"),
    )
    hero = await entities.create_character(
        campaign_id,
        CharacterCreate(canonical_name="Вера", current_location_id=tavern.id),
    )
    await campaigns.update(campaign_id, CampaignUpdate(player_character_id=hero.id))
    source = await scenes.create(
        campaign_id,
        SceneCreate(title="Утро", location_id=tavern.id),
    )
    await scenes.add_participant(source.id, hero.id, allow_movement=True)
    await SceneLifecycleService(db_session).activate(campaign_id, source.id)
    await SceneStateService(db_session).create_exit(
        campaign_id,
        tavern.id,
        LocationExitCreate(to_location_id=street.id, label="наружу"),
    )
    user = await TurnRepository(db_session).create(
        campaign_id,
        TurnCreate(
            role="user",
            scene_id=source.id,
            content="Киваю хозяину и иду к выходу, если он открыт.",
        ),
    )

    authorization = await PlayerDestinationAuthorizer(db_session).authorize(
        user.id,
        tavern.canonical_name,
    )

    assert authorization.authorized is True
    assert authorization.destination == street.canonical_name
    assert "unique available exit" in authorization.reason


@pytest.mark.asyncio
async def test_outward_phrase_does_not_take_unique_reverse_exit(
    db_session: AsyncSession,
):
    from app.db.repositories.turn_repo import TurnRepository
    from app.models.scene_state import LocationExitCreate
    from app.models.turn import TurnCreate
    from app.services.player_destination_authorization import PlayerDestinationAuthorizer
    from app.services.scene_state_service import SceneStateService

    campaign_id = uuid4()
    campaigns = CampaignRepository(db_session)
    entities = EntityRepository(db_session)
    locations = LocationRepository(db_session)
    scenes = SceneRepository(db_session)

    await campaigns.create(campaign_id, CampaignCreate(name="No reverse outside"))
    tavern = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Трактир «Якорь»"),
    )
    street = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Окрестности — Трактир «Якорь»"),
    )
    hero = await entities.create_character(
        campaign_id,
        CharacterCreate(canonical_name="Вера", current_location_id=street.id),
    )
    await campaigns.update(campaign_id, CampaignUpdate(player_character_id=hero.id))
    source = await scenes.create(
        campaign_id,
        SceneCreate(title="Улица", location_id=street.id),
    )
    await scenes.add_participant(source.id, hero.id, allow_movement=True)
    await SceneLifecycleService(db_session).activate(campaign_id, source.id)
    await SceneStateService(db_session).create_exit(
        campaign_id,
        street.id,
        LocationExitCreate(to_location_id=tavern.id, label="обратно"),
    )
    user = await TurnRepository(db_session).create(
        campaign_id,
        TurnCreate(
            role="user",
            scene_id=source.id,
            content="Выхожу наружу.",
        ),
    )

    authorization = await PlayerDestinationAuthorizer(db_session).authorize(
        user.id,
        street.canonical_name,
    )

    assert authorization.destination != tavern.canonical_name
    assert authorization.authorized is False or authorization.destination == street.canonical_name


@pytest.mark.asyncio
async def test_return_phrase_takes_unique_reverse_exit(
    db_session: AsyncSession,
):
    from app.db.repositories.turn_repo import TurnRepository
    from app.models.scene_state import LocationExitCreate
    from app.models.turn import TurnCreate
    from app.services.player_destination_authorization import PlayerDestinationAuthorizer
    from app.services.scene_state_service import SceneStateService

    campaign_id = uuid4()
    campaigns = CampaignRepository(db_session)
    entities = EntityRepository(db_session)
    locations = LocationRepository(db_session)
    scenes = SceneRepository(db_session)

    await campaigns.create(campaign_id, CampaignCreate(name="Return unique reverse"))
    tavern = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Трактир «Якорь»"),
    )
    street = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Улица"),
    )
    invented = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Окрестности — Улица"),
    )
    hero = await entities.create_character(
        campaign_id,
        CharacterCreate(canonical_name="Вера", current_location_id=street.id),
    )
    await campaigns.update(campaign_id, CampaignUpdate(player_character_id=hero.id))
    source = await scenes.create(
        campaign_id,
        SceneCreate(title="Улица", location_id=street.id),
    )
    await scenes.add_participant(source.id, hero.id, allow_movement=True)
    await SceneLifecycleService(db_session).activate(campaign_id, source.id)
    await SceneStateService(db_session).create_exit(
        campaign_id,
        street.id,
        LocationExitCreate(to_location_id=tavern.id, label="обратно"),
    )
    user = await TurnRepository(db_session).create(
        campaign_id,
        TurnCreate(
            role="user",
            scene_id=source.id,
            content="Возвращаюсь туда, откуда только что пришла.",
        ),
    )

    authorization = await PlayerDestinationAuthorizer(db_session).authorize(
        user.id,
        invented.canonical_name,
    )

    assert authorization.authorized is True
    assert authorization.destination == tavern.canonical_name
    assert "unique available exit from return clause" in authorization.reason


async def _nested_outside_topology(db_session: AsyncSession, campaign_name: str):
    """Tavern with innkeeper → outskirts → nested extra-outside. Player is on the nested street."""
    from app.db.repositories.turn_repo import TurnRepository
    from app.models.turn import TurnCreate

    campaign_id = uuid4()
    campaigns = CampaignRepository(db_session)
    entities = EntityRepository(db_session)
    locations = LocationRepository(db_session)
    scenes = SceneRepository(db_session)
    executor = SceneTransitionExecutor(db_session)

    await campaigns.create(campaign_id, CampaignCreate(name=campaign_name))
    tavern = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Трактир «Якорь»"),
    )
    hero = await entities.create_character(
        campaign_id,
        CharacterCreate(canonical_name="Вера", current_location_id=tavern.id),
    )
    innkeeper = await entities.create_character(
        campaign_id,
        CharacterCreate(
            canonical_name="Хозяин трактира",
            current_location_id=tavern.id,
        ),
    )
    await campaigns.update(campaign_id, CampaignUpdate(player_character_id=hero.id))
    tavern_scene = await scenes.create(
        campaign_id,
        SceneCreate(title="Начало: Трактир «Якорь»", location_id=tavern.id),
    )
    await scenes.add_participant(tavern_scene.id, hero.id, allow_movement=True)
    await scenes.add_participant(tavern_scene.id, innkeeper.id, allow_movement=False)
    await SceneLifecycleService(db_session).activate(campaign_id, tavern_scene.id)

    leave_outskirts = await executor.apply(
        campaign_id,
        tavern_scene.id,
        None,
        SceneTransitionPlan(
            required=True,
            transition_type="location_transition",
            destination_location="Окрестности — Трактир «Якорь»",
            scene_title="Окрестности — Трактир «Якорь»",
            carry_participants=["Вера"],
            reason="Игрок выходит наружу.",
        ),
        allow_route_discovery=True,
    )
    assert leave_outskirts is not None
    outskirts_id = leave_outskirts.target_location_id
    assert outskirts_id != tavern.id

    leave_nested = await executor.apply(
        campaign_id,
        leave_outskirts.target_scene_id,
        None,
        SceneTransitionPlan(
            required=True,
            transition_type="location_transition",
            destination_location=(
                "Окрестности — Трактир «Якорь» — "
                "Ближайшее обычное пространство за пределами стартовой сцены"
            ),
            scene_title=(
                "Окрестности — Трактир «Якорь» — "
                "Ближайшее обычное пространство за пределами стартовой сцены"
            ),
            carry_participants=["Вера"],
            reason="Игрок идёт ещё дальше наружу.",
        ),
        allow_route_discovery=True,
    )
    assert leave_nested is not None
    assert leave_nested.target_location_id not in {tavern.id, outskirts_id}

    user = await TurnRepository(db_session).create(
        campaign_id,
        TurnCreate(
            role="user",
            scene_id=leave_nested.target_scene_id,
            content="Возвращаюсь туда, откуда только что пришла.",
        ),
    )
    return {
        "campaign_id": campaign_id,
        "scenes": scenes,
        "executor": executor,
        "tavern": tavern,
        "innkeeper": innkeeper,
        "outskirts_id": outskirts_id,
        "nested_scene_id": leave_nested.target_scene_id,
        "user": user,
    }


def _live_return_plan() -> SceneTransitionPlan:
    return SceneTransitionPlan(
        required=True,
        transition_type="location_transition",
        destination_location="Окрестности — Трактир «Якорь»",
        scene_title="Трактир «Якорь»",
        carry_participants=["Вера"],
        reason="Исходное место",
    )


@pytest.mark.asyncio
async def test_apply_return_after_nested_outside_restores_original_tavern(
    db_session: AsyncSession,
):
    """Live topology: tavern → outskirts → extra-outside, then anaphoric return.

    Unique reverse from the nested street is the empty outskirts. Apply must still
    land on the original tavern and restore the innkeeper who stayed there.
    """
    world = await _nested_outside_topology(db_session, "Nested return apply")
    tavern = world["tavern"]
    innkeeper = world["innkeeper"]
    scenes = world["scenes"]

    returned = await world["executor"].apply(
        world["campaign_id"],
        world["nested_scene_id"],
        world["user"].id,
        _live_return_plan(),
    )

    assert returned is not None
    assert returned.target_location_id == tavern.id
    target = await scenes.get_by_id(returned.target_scene_id)
    assert target is not None
    assert target.location_id == tavern.id
    assert innkeeper.id in target.participants
    assert target.title == tavern.canonical_name


@pytest.mark.asyncio
async def test_sequence_return_after_nested_outside_restores_original_tavern(
    db_session: AsyncSession,
):
    """Same live topology through ActionSequenceExecutor (inner apply has no trigger turn)."""
    from app.services.action_sequence_executor import ActionSequenceExecutor
    from app.services.turn_planner import ActionSequencePlan, ActionStepPlan

    world = await _nested_outside_topology(db_session, "Nested return sequence")
    tavern = world["tavern"]
    innkeeper = world["innkeeper"]
    scenes = world["scenes"]

    execution = await ActionSequenceExecutor(db_session).execute(
        world["campaign_id"],
        world["nested_scene_id"],
        world["user"].id,
        ActionSequencePlan(
            steps=[
                ActionStepPlan(
                    action_type="movement",
                    intent="возвращение в трактир «Якорь»",
                    resolution="auto_success",
                    safe_mundane=True,
                    observable_outcome=(
                        "Ты направляешься обратно к трактиру «Якорь»."
                    ),
                    transition=_live_return_plan(),
                )
            ]
        ),
        route_discovery_turn_id=world["user"].id,
    )

    assert execution.final_scene_id is not None
    target = await scenes.get_by_id(execution.final_scene_id)
    assert target is not None
    assert target.location_id == tavern.id
    assert innkeeper.id in target.participants
    assert target.title == tavern.canonical_name
