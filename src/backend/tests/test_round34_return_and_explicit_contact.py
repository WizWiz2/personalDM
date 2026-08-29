from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

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
from app.models.turn import TurnCreate
from app.services.player_destination_authorization import PlayerDestinationAuthorizer
from app.services.scene_lifecycle import SceneLifecycleService
from app.services.turn_authority_planner import CoordinatedTurnPlan, TurnAuthorityPlanner
from app.services.turn_planner import ActionSequencePlan, ActionStepPlan


@pytest.mark.asyncio
async def test_live_return_ignores_trailing_punctuation_in_visited_location_identity(
    db_session: AsyncSession,
):
    campaign_id = uuid4()
    campaigns = CampaignRepository(db_session)
    entities = EntityRepository(db_session)
    locations = LocationRepository(db_session)
    scenes = SceneRepository(db_session)

    await campaigns.create(campaign_id, CampaignCreate(name="Round 34 live return"))
    office = await locations.create(
        campaign_id,
        LocationCreate(
            canonical_name="Небольшой частный детективный офис в центре города."
        ),
    )
    await locations.create(
        campaign_id,
        LocationCreate(
            canonical_name="Окрестности — Небольшой частный детективный офис в центре города."
        ),
    )
    cafe = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Угол Светлой — кафе Полуночный кофе"),
    )
    house = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Улица Светлой — Дом владельца"),
    )
    hero = await entities.create_character(
        campaign_id,
        CharacterCreate(canonical_name="Марк", current_location_id=house.id),
    )
    await campaigns.update(campaign_id, CampaignUpdate(player_character_id=hero.id))

    office_scene = await scenes.create(
        campaign_id,
        SceneCreate(title="Офис", location_id=office.id),
    )
    cafe_scene = await scenes.create(
        campaign_id,
        SceneCreate(title="Кафе", location_id=cafe.id),
    )
    house_scene = await scenes.create(
        campaign_id,
        SceneCreate(title="Дом владельца", location_id=house.id),
    )
    await scenes.add_participant(house_scene.id, hero.id, allow_movement=True)
    await SceneLifecycleService(db_session).activate(campaign_id, house_scene.id)

    db_session.add_all(
        [
            SceneTransition(
                campaign_id=str(campaign_id),
                source_scene_id=str(office_scene.id),
                target_scene_id=str(cafe_scene.id),
                transition_type="location_transition",
                status="applied",
                source_location_id=str(office.id),
                target_location_id=str(cafe.id),
            ),
            SceneTransition(
                campaign_id=str(campaign_id),
                source_scene_id=str(cafe_scene.id),
                target_scene_id=str(house_scene.id),
                transition_type="location_transition",
                status="applied",
                source_location_id=str(cafe.id),
                target_location_id=str(house.id),
            ),
        ]
    )
    await db_session.flush()

    user = await TurnRepository(db_session).create(
        campaign_id,
        TurnCreate(role="user", scene_id=house_scene.id, content="Возвращаюсь в офис."),
    )
    authorization = await PlayerDestinationAuthorizer(db_session).authorize(
        user.id,
        "Небольшой частный детективный офис в центре города",
    )

    assert authorization.applicable is True
    assert authorization.authorized is True
    assert authorization.destination_exists is True
    assert authorization.destination == "Небольшой частный детективный офис в центре города"
    assert "previously visited physical location" in authorization.reason


def _contact_plan(*, consequence: str | None, resolution: str = "auto_success"):
    return CoordinatedTurnPlan(
        player_intent="расспросить прохожего о подозрительных людях",
        resolution="sequence",
        action_sequence=ActionSequencePlan(
            steps=[
                ActionStepPlan(
                    action_type="interaction",
                    intent="расспросить прохожего о подозрительных людях",
                    resolution=resolution,
                    safe_mundane=resolution == "auto_success",
                    observable_outcome=None,
                    blocking_reason=(
                        "Никого подходящего рядом нет."
                        if resolution != "auto_success"
                        else None
                    ),
                )
            ]
        ),
        observable_consequences=[consequence] if consequence else [],
        npc_introductions=[],
    )


def test_affirmative_auto_success_contact_gets_typed_temporary_identity():
    player_input = (
        "На улице расспрашиваю прохожего, не видел ли он кого-нибудь подозрительного."
    )
    consequence = (
        "Прохожий, мужчина средних лет, отвечает, что ничего подозрительного не замечал."
    )
    plan = _contact_plan(consequence=consequence)

    TurnAuthorityPlanner.normalize_affirmative_direct_contact(plan, player_input)

    assert len(plan.npc_introductions) == 1
    intro = plan.npc_introductions[0]
    assert intro.canonical_name == "Прохожий"
    assert intro.role == "прохожий"
    assert intro.temporary_name is True
    assert plan.action_sequence.steps[0].observable_outcome == consequence
    assert TurnAuthorityPlanner.contract_issues(plan, player_input) == []


def test_explicit_negative_contact_does_not_force_materialization():
    player_input = "Расспрашиваю прохожего, не видел ли он машину."
    plan = _contact_plan(consequence="Никто из прохожих не останавливается.")

    TurnAuthorityPlanner.normalize_affirmative_direct_contact(plan, player_input)

    assert plan.npc_introductions == []
    assert plan.action_sequence.steps[0].observable_outcome is None
    assert TurnAuthorityPlanner.contract_issues(plan, player_input) == []


def test_unresolved_contact_without_positive_outcome_is_not_forced_to_succeed():
    player_input = "Расспрашиваю прохожего, не видел ли он машину."
    plan = _contact_plan(consequence=None)

    TurnAuthorityPlanner.normalize_affirmative_direct_contact(plan, player_input)

    assert plan.npc_introductions == []
    assert plan.action_sequence.steps[0].observable_outcome is None
    assert any(
        "direct contact" in issue.casefold()
        for issue in TurnAuthorityPlanner.contract_issues(plan, player_input)
    )


def test_blocked_contact_is_not_promoted_even_with_stale_positive_prose():
    player_input = "Расспрашиваю прохожего, не видел ли он машину."
    plan = _contact_plan(
        consequence="Прохожий отвечает на вопрос.",
        resolution="blocked",
    )

    TurnAuthorityPlanner.normalize_affirmative_direct_contact(plan, player_input)

    assert plan.npc_introductions == []
