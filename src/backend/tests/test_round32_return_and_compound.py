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
from app.models.turn_authority import PlannedNpcIntroduction
from app.services.player_destination_authorization import PlayerDestinationAuthorizer
from app.services.scene_lifecycle import SceneLifecycleService
from app.services.systemless_authority_guard import (
    sanitize_player_premise_npc_introductions,
    systemless_contract_issues,
)
from app.services.turn_authority_planner import CoordinatedTurnPlan
from app.services.turn_planner import ActionSequencePlan, ActionStepPlan


async def _campaign_with_location_history(db_session: AsyncSession):
    campaign_id = uuid4()
    campaigns = CampaignRepository(db_session)
    entities = EntityRepository(db_session)
    locations = LocationRepository(db_session)
    scenes = SceneRepository(db_session)

    await campaigns.create(campaign_id, CampaignCreate(name="Round 32 return history"))
    office = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Небольшой частный детективный офис в центре города"),
    )
    # This bootstrap-only location deliberately contains the same generic noun but was never visited.
    outskirts = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Окрестности — небольшой частный детективный офис"),
    )
    diner = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Портовый проспект — забегаловка"),
    )
    house = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Портовый проспект — дом владельца"),
    )
    hero = await entities.create_character(
        campaign_id,
        CharacterCreate(canonical_name="Алексей", current_location_id=house.id),
    )
    await campaigns.update(campaign_id, CampaignUpdate(player_character_id=hero.id))

    office_scene = await scenes.create(
        campaign_id,
        SceneCreate(title="Офис", location_id=office.id),
    )
    diner_scene = await scenes.create(
        campaign_id,
        SceneCreate(title="Забегаловка", location_id=diner.id),
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
                target_scene_id=str(diner_scene.id),
                transition_type="location_transition",
                status="prepared",
                source_location_id=str(office.id),
                target_location_id=str(diner.id),
            ),
            SceneTransition(
                campaign_id=str(campaign_id),
                source_scene_id=str(diner_scene.id),
                target_scene_id=str(house_scene.id),
                transition_type="location_transition",
                status="prepared",
                source_location_id=str(diner.id),
                target_location_id=str(house.id),
            ),
        ]
    )
    await db_session.flush()
    return campaign_id, office, outskirts, diner, house, house_scene


@pytest.mark.asyncio
async def test_return_can_resolve_unique_previously_visited_location(
    db_session: AsyncSession,
):
    _, office, _, _, _, house_scene = await _campaign_with_location_history(db_session)
    user = await TurnRepository(db_session).create(
        house_scene.campaign_id,
        TurnCreate(
            role="user",
            scene_id=house_scene.id,
            content="Возвращаюсь в офис.",
        ),
    )

    authorization = await PlayerDestinationAuthorizer(db_session).authorize(
        user.id,
        office.canonical_name,
    )

    assert authorization.applicable is True
    assert authorization.authorized is True
    assert authorization.destination_exists is True
    assert "previously visited physical location" in authorization.reason


def _plan_with_intro(introduction: PlannedNpcIntroduction) -> CoordinatedTurnPlan:
    return CoordinatedTurnPlan(
        player_intent="осмотреть дверь, попробовать открыть и при успехе спуститься",
        resolution="sequence",
        action_sequence=ActionSequencePlan(
            steps=[
                ActionStepPlan(
                    action_type="observation",
                    intent="осмотреть дверь",
                    resolution="auto_success",
                    safe_mundane=True,
                    observable_outcome="Дверь осмотрена.",
                ),
                ActionStepPlan(
                    action_type="interaction",
                    intent="попробовать открыть дверь",
                    resolution="blocked",
                    blocking_reason="Дверь заперта.",
                ),
            ]
        ),
        npc_introductions=[introduction],
    )


def test_lowercase_player_premise_is_not_treated_as_new_npc():
    player_input = "Осматриваю дверь, пробую её открыть и, если получится, спускаюсь вниз."
    plan = _plan_with_intro(
        PlannedNpcIntroduction(
            canonical_name="Дверь",
            role="дверь",
            temporary_name=True,
            reason="Упомянута игроком в ходе.",
        )
    )

    sanitize_player_premise_npc_introductions(plan, player_input)
    issues = systemless_contract_issues(plan, player_input)

    assert plan.npc_introductions == []
    assert not any("new physical NPC introductions" in issue for issue in issues)
    assert len(plan.action_sequence.steps) == 2


def test_genuinely_invented_unsolicited_character_still_fails_closed():
    player_input = "Осматриваю дверь и пробую её открыть."
    plan = _plan_with_intro(
        PlannedNpcIntroduction(
            canonical_name="Незнакомец",
            role="человек в коридоре",
            temporary_name=True,
            reason="Planner решил добавить свидетеля.",
        )
    )

    issues = systemless_contract_issues(plan, player_input)

    assert len(plan.npc_introductions) == 1
    assert any("new physical NPC introductions" in issue for issue in issues)


def test_explicit_unknown_contact_is_not_sanitized_as_common_noun():
    player_input = "Расспрашиваю прохожего, не видел ли он ночью машину."
    plan = CoordinatedTurnPlan(
        player_intent="расспросить прохожего",
        resolution="conversation",
        npc_introductions=[
            PlannedNpcIntroduction(
                canonical_name="Прохожий",
                role="прохожий",
                temporary_name=True,
                reason="Игрок прямо обратился к неизвестному прохожему.",
            )
        ],
    )

    issues = systemless_contract_issues(plan, player_input)

    assert len(plan.npc_introductions) == 1
    assert not any("new physical NPC introductions" in issue for issue in issues)
