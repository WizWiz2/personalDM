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
from app.services.player_destination_authorization import PlayerDestinationAuthorizer
from app.services.scene_lifecycle import SceneLifecycleService
from app.services.scene_state_service import SceneStateService
from app.services.scene_transition_executor import SceneTransitionExecutor
from app.services.turn_planner import ActionSequencePlan, ActionStepPlan, SceneTransitionPlan


async def _world(db_session: AsyncSession):
    campaign_id = uuid4()
    campaigns = CampaignRepository(db_session)
    entities = EntityRepository(db_session)
    locations = LocationRepository(db_session)
    scenes = SceneRepository(db_session)

    await campaigns.create(campaign_id, CampaignCreate(name="Round 11 navigation"))
    office = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Detective Office"),
    )
    street = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Lower City Street"),
    )
    cyber = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Cybercrime Department"),
    )
    investigation = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Investigation Department"),
    )
    bar = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="The Bar Old Mug"),
    )
    apartment_building = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Apartment Building on Lenina"),
    )
    suspect_apartment = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Suspect Apartment 47"),
    )
    merchants = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Merchant Quarter"),
    )
    basement = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Locked Basement"),
    )

    hero = await entities.create_character(
        campaign_id,
        CharacterCreate(canonical_name="Rat", current_location_id=office.id),
    )
    await campaigns.update(
        campaign_id,
        CampaignUpdate(player_character_id=hero.id),
    )
    source = await scenes.create(
        campaign_id,
        SceneCreate(title="Detective Office", location_id=office.id),
    )
    await SceneLifecycleService(db_session).activate(campaign_id, source.id)

    state = SceneStateService(db_session)
    await state.create_exit(
        campaign_id,
        office.id,
        LocationExitCreate(to_location_id=street.id, label="Street"),
    )
    await state.create_exit(
        campaign_id,
        office.id,
        LocationExitCreate(to_location_id=cyber.id, label="Cybercrime Department"),
    )
    await state.create_exit(
        campaign_id,
        bar.id,
        LocationExitCreate(
            to_location_id=basement.id,
            label="Locked basement",
            access_rule="sealed",
            active=False,
        ),
    )
    await db_session.commit()
    return {
        "campaign_id": campaign_id,
        "hero": hero,
        "office": office,
        "street": street,
        "cyber": cyber,
        "investigation": investigation,
        "bar": bar,
        "apartment_building": apartment_building,
        "suspect_apartment": suspect_apartment,
        "merchants": merchants,
        "basement": basement,
        "source": source,
    }


@pytest.mark.asyncio
async def test_ambiguous_generic_destination_is_blocked_even_when_one_route_exists(
    db_session: AsyncSession,
):
    world = await _world(db_session)
    turn = await TurnRepository(db_session).create(
        world["campaign_id"],
        TurnCreate(role="user", content="Go to the Department."),
    )

    with pytest.raises(ValueError, match="destination reference is ambiguous"):
        await SceneTransitionExecutor(db_session).apply(
            world["campaign_id"],
            world["source"].id,
            turn.id,
            SceneTransitionPlan(
                required=True,
                transition_type="location_transition",
                destination_location=world["cyber"].canonical_name,
            ),
        )

    hero = await EntityRepository(db_session).get_character(world["hero"].id)
    assert hero is not None
    assert hero.current_location_id == world["office"].id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "destination_key"),
    [
        ("Get to Apartment Building on Lenina.", "apartment_building"),
        ("Enter suspect's apartment #47.", "suspect_apartment"),
    ],
)
async def test_natural_travel_forms_authorize_named_destination(
    db_session: AsyncSession,
    content: str,
    destination_key: str,
):
    world = await _world(db_session)
    destination = world[destination_key]
    turn = await TurnRepository(db_session).create(
        world["campaign_id"],
        TurnCreate(role="user", content=content),
    )

    applied = await SceneTransitionExecutor(db_session).apply(
        world["campaign_id"],
        world["source"].id,
        turn.id,
        SceneTransitionPlan(
            required=True,
            transition_type="location_transition",
            destination_location=destination.canonical_name,
        ),
    )

    assert applied is not None
    assert applied.target_location_id == destination.id
    hero = await EntityRepository(db_session).get_character(world["hero"].id)
    assert hero is not None
    assert hero.current_location_id == destination.id


@pytest.mark.asyncio
async def test_compound_travel_and_observation_reaches_named_department(
    db_session: AsyncSession,
):
    world = await _world(db_session)
    turn = await TurnRepository(db_session).create(
        world["campaign_id"],
        TurnCreate(
            role="user",
            content="Go to Investigation Department, go inside and examine the lobby.",
        ),
    )
    sequence = ActionSequencePlan(
        summary="Travel to Investigation Department and inspect the lobby.",
        steps=[
            ActionStepPlan(
                action_type="movement",
                intent="Go to Investigation Department",
                resolution="auto_success",
                safe_mundane=True,
                observable_outcome="Rat reaches Investigation Department.",
                transition=SceneTransitionPlan(
                    required=True,
                    transition_type="location_transition",
                    destination_location=world["investigation"].canonical_name,
                ),
            ),
            ActionStepPlan(
                action_type="observation",
                intent="Examine the lobby",
                resolution="auto_success",
                safe_mundane=True,
                observable_outcome="Rat examines the lobby.",
            ),
        ],
    )

    applied = await SceneTransitionExecutor(db_session).apply(
        world["campaign_id"],
        world["source"].id,
        turn.id,
        SceneTransitionPlan(
            required=True,
            transition_type="focus_transition",
            sequence_payload=sequence.model_dump(mode="json"),
        ),
    )

    assert applied is not None
    assert applied.action_sequence is not None
    assert applied.action_sequence.blocked_step_index is None
    assert [step.status for step in applied.action_sequence.steps] == [
        "completed",
        "completed",
    ]
    assert applied.target_location_id == world["investigation"].id


@pytest.mark.asyncio
async def test_return_clause_after_observation_is_authorized_independently(
    db_session: AsyncSession,
):
    world = await _world(db_session)
    turn = await TurnRepository(db_session).create(
        world["campaign_id"],
        TurnCreate(
            role="user",
            content="Examine the bedroom and return to The Bar Old Mug.",
        ),
    )
    authorization = await PlayerDestinationAuthorizer(db_session).authorize(
        turn.id,
        world["bar"].canonical_name,
    )

    assert authorization.applicable is True
    assert authorization.authorized is True
    assert authorization.matched_clause is not None
    assert "return" in authorization.matched_clause


@pytest.mark.asyncio
async def test_anaphoric_travel_resolves_from_committed_destination(
    db_session: AsyncSession,
):
    world = await _world(db_session)
    room = await LocationRepository(db_session).create(
        world["campaign_id"],
        LocationCreate(canonical_name="Guest Room 3"),
    )
    turn = await TurnRepository(db_session).create(
        world["campaign_id"],
        TurnCreate(role="user", content="I rent the room and go there to sleep."),
    )
    authorization = await PlayerDestinationAuthorizer(db_session).authorize(
        turn.id,
        room.canonical_name,
    )

    assert authorization.applicable is True
    assert authorization.authorized is True
    assert "anaphoric" in authorization.reason

    applied = await SceneTransitionExecutor(db_session).apply(
        world["campaign_id"],
        world["source"].id,
        turn.id,
        SceneTransitionPlan(
            required=True,
            transition_type="location_transition",
            destination_location=room.canonical_name,
        ),
    )
    assert applied is not None
    assert applied.target_location_id == room.id


@pytest.mark.asyncio
async def test_unresolved_destination_cannot_discover_missing_route(
    db_session: AsyncSession,
):
    world = await _world(db_session)
    room = await LocationRepository(db_session).create(
        world["campaign_id"],
        LocationCreate(canonical_name="Guest Room 3"),
    )
    turn = await TurnRepository(db_session).create(
        world["campaign_id"],
        TurnCreate(role="user", content="I rent the room and sleep."),
    )
    authorization = await PlayerDestinationAuthorizer(db_session).authorize(
        turn.id,
        room.canonical_name,
    )
    assert authorization.applicable is False

    with pytest.raises(ValueError, match="existing route is required"):
        await SceneTransitionExecutor(db_session).apply(
            world["campaign_id"],
            world["source"].id,
            turn.id,
            SceneTransitionPlan(
                required=True,
                transition_type="location_transition",
                destination_location=room.canonical_name,
            ),
        )


@pytest.mark.asyncio
async def test_unresolved_destination_cannot_create_new_location(
    db_session: AsyncSession,
):
    world = await _world(db_session)
    turn = await TurnRepository(db_session).create(
        world["campaign_id"],
        TurnCreate(role="user", content="I go there and look around."),
    )

    with pytest.raises(ValueError, match="new location cannot be created"):
        await SceneTransitionExecutor(db_session).apply(
            world["campaign_id"],
            world["source"].id,
            turn.id,
            SceneTransitionPlan(
                required=True,
                transition_type="location_transition",
                destination_location="Secret Annex",
            ),
        )

    locations = await LocationRepository(db_session).list_by_campaign(
        world["campaign_id"]
    )
    assert not any(item.canonical_name == "Secret Annex" for item in locations)


@pytest.mark.asyncio
async def test_scope_trap_authorizes_merchants_but_not_basement(
    db_session: AsyncSession,
):
    world = await _world(db_session)
    turn = await TurnRepository(db_session).create(
        world["campaign_id"],
        TurnCreate(
            role="user",
            content="Go to the Merchants, then wonder what's in the locked basement.",
        ),
    )
    authorizer = PlayerDestinationAuthorizer(db_session)

    merchants = await authorizer.authorize(
        turn.id,
        world["merchants"].canonical_name,
    )
    basement = await authorizer.authorize(
        turn.id,
        world["basement"].canonical_name,
    )

    assert merchants.authorized is True
    assert basement.applicable is True
    assert basement.authorized is False
    assert basement.reason == "destination is only mentioned in a non-committal clause"


@pytest.mark.asyncio
async def test_scope_trap_sequence_stops_after_named_destination(
    db_session: AsyncSession,
):
    world = await _world(db_session)
    turn = await TurnRepository(db_session).create(
        world["campaign_id"],
        TurnCreate(
            role="user",
            content="Go to the Merchants, then wonder what's in the locked basement.",
        ),
    )
    sequence = ActionSequencePlan(
        summary="Go to the Merchants and consider the basement.",
        steps=[
            ActionStepPlan(
                action_type="movement",
                intent="Go to Merchant Quarter",
                resolution="auto_success",
                safe_mundane=True,
                observable_outcome="Rat reaches Merchant Quarter.",
                transition=SceneTransitionPlan(
                    required=True,
                    transition_type="location_transition",
                    destination_location=world["merchants"].canonical_name,
                ),
            ),
            ActionStepPlan(
                action_type="movement",
                intent="Go to Locked Basement",
                resolution="auto_success",
                safe_mundane=True,
                observable_outcome="Rat reaches Locked Basement.",
                transition=SceneTransitionPlan(
                    required=True,
                    transition_type="location_transition",
                    destination_location=world["basement"].canonical_name,
                ),
            ),
        ],
    )

    applied = await SceneTransitionExecutor(db_session).apply(
        world["campaign_id"],
        world["source"].id,
        turn.id,
        SceneTransitionPlan(
            required=True,
            transition_type="focus_transition",
            sequence_payload=sequence.model_dump(mode="json"),
        ),
    )

    assert applied is not None
    assert applied.action_sequence is not None
    assert [step.status for step in applied.action_sequence.steps] == [
        "completed",
        "blocked",
    ]
    assert applied.action_sequence.blocked_step_index == 1
    assert applied.target_location_id == world["merchants"].id
    assert "non-committal" in (
        applied.action_sequence.steps[1].blocking_reason or ""
    )
