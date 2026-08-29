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
from app.services.turn_authority_planner import TurnAuthorityPlanner


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
    campaign_id, office, _, _, _, house_scene = await _campaign_with_location_history(db_session)
    user = await TurnRepository(db_session).create(
        campaign_id,
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


def test_object_mentions_must_not_be_reinterpreted_as_people_by_semantic_reviewer():
    prompt = TurnAuthorityPlanner.SEMANTIC_REVIEW_PROMPT

    assert "ENTITY TYPE" in prompt
    assert "objects, symbols, clues, doors" in prompt
    assert "npc_introduction" in prompt
    assert "Do not use keyword lists" in prompt


def test_unsolicited_new_physical_character_requires_typed_authority():
    prompt = TurnAuthorityPlanner.SEMANTIC_REVIEW_PROMPT

    assert "CONTACT/IDENTITY" in prompt
    assert "unknown physical responder" in prompt
    assert "npc_introductions" in prompt


def test_direct_unknown_contact_can_be_typed_without_lexical_sanitizer():
    prompt = TurnAuthorityPlanner.AUTHORITY_ADDENDUM

    assert "NPC / ENTITY AUTHORITY" in prompt
    assert "previously unknown person physically appears" in prompt
    assert "npc_introductions" in prompt
