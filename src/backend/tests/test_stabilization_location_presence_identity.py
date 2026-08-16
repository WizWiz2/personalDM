from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.location_repo import LocationRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.repositories.turn_repo import TurnRepository
from app.db.tables import Character
from app.models.campaign import CampaignCreate, CampaignUpdate
from app.models.character import CharacterCreate
from app.models.location import LocationCreate
from app.models.scene import SceneCreate
from app.models.scene_state import LocationExitCreate
from app.models.turn import TurnCreate
from app.services.location_identity import location_reference_key, same_location_reference
from app.services.scene_lifecycle import SceneLifecycleService
from app.services.scene_state_service import SceneStateService
from app.services.scene_transition_executor import SceneTransitionExecutor
from app.services.turn_planner import SceneTransitionPlan


def test_numbered_location_reference_normalizes_notation_and_word_order():
    assert location_reference_key("Причал №7") == ("7", "prichal")
    assert location_reference_key("Причал номер семь") == ("7", "prichal")
    assert location_reference_key("Седьмой причал") == ("7", "prichal")
    assert same_location_reference("Седьмой причал", "Причал №7") is True
    assert same_location_reference("Восьмой причал", "Причал №7") is False


@pytest.mark.asyncio
async def test_revisit_uses_existing_route_identity_and_restores_known_resident(
    db_session: AsyncSession,
):
    campaign_id = uuid4()
    campaigns = CampaignRepository(db_session)
    entities = EntityRepository(db_session)
    locations = LocationRepository(db_session)
    scenes = SceneRepository(db_session)
    state = SceneStateService(db_session)
    turns = TurnRepository(db_session)

    await campaigns.create(campaign_id, CampaignCreate(name="Round 22 revisit"))
    tavern = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Портовой трактир"),
    )
    pier = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Причал номер семь"),
    )
    hero = await entities.create_character(
        campaign_id,
        CharacterCreate(canonical_name="Мария"),
    )
    dockworker = await entities.create_character(
        campaign_id,
        CharacterCreate(canonical_name="Грузчик"),
    )
    hidden_resident = await entities.create_character(
        campaign_id,
        CharacterCreate(canonical_name="Неизвестный наблюдатель"),
    )
    await campaigns.update(
        campaign_id,
        CampaignUpdate(player_character_id=hero.id),
    )

    # The dockworker has actually been presented at the pier before. The hidden resident only has
    # physical location state and must not be surfaced just because the player revisits the place.
    old_pier_scene = await scenes.create(
        campaign_id,
        SceneCreate(title="Причал номер семь", location_id=pier.id),
    )
    await scenes.add_participant(old_pier_scene.id, hero.id)
    await scenes.add_participant(old_pier_scene.id, dockworker.id)
    hidden_row = await db_session.get(Character, str(hidden_resident.id))
    assert hidden_row is not None
    hidden_row.current_location_id = str(pier.id)
    await db_session.flush()
    await SceneLifecycleService(db_session).activate(campaign_id, old_pier_scene.id)

    tavern_scene = await scenes.create(
        campaign_id,
        SceneCreate(title="Портовой трактир", location_id=tavern.id),
    )
    await scenes.add_participant(tavern_scene.id, hero.id, allow_movement=True)
    await SceneLifecycleService(db_session).activate(campaign_id, tavern_scene.id)

    await state.create_exit(
        campaign_id,
        tavern.id,
        LocationExitCreate(
            to_location_id=pier.id,
            label="Причал №7",
            reverse_label="Портовой трактир",
            bidirectional=True,
            discovered=True,
            active=True,
        ),
    )
    user_turn = await turns.create(
        campaign_id,
        TurnCreate(
            role="user",
            content="Возвращаюсь на седьмой причал.",
            scene_id=tavern_scene.id,
        ),
    )
    await db_session.commit()

    before_locations = await locations.list_by_campaign(campaign_id)
    result = await SceneTransitionExecutor(db_session).apply(
        campaign_id,
        tavern_scene.id,
        user_turn.id,
        SceneTransitionPlan(
            required=True,
            transition_type="location_transition",
            destination_location="Седьмой причал",
            scene_title="Седьмой причал у набережной",
            carry_participants=[],
            reason="Мария возвращается на уже известный причал.",
        ),
    )

    assert result is not None
    assert result.target_location_id == pier.id
    assert result.target_scene_id != old_pier_scene.id
    assert len(await locations.list_by_campaign(campaign_id)) == len(before_locations)
    assert not any(
        location.canonical_name == "Седьмой причал"
        for location in await locations.list_by_campaign(campaign_id)
    )

    target = await scenes.get_by_id(result.target_scene_id)
    assert target is not None
    assert set(target.participants) == {hero.id, dockworker.id}
    assert hidden_resident.id not in target.participants

    dockworker_row = await db_session.get(Character, str(dockworker.id))
    assert dockworker_row is not None
    assert dockworker_row.current_location_id == str(pier.id)


@pytest.mark.asyncio
async def test_route_scoped_identity_does_not_merge_unrelated_numbered_locations(
    db_session: AsyncSession,
):
    campaign_id = uuid4()
    campaigns = CampaignRepository(db_session)
    locations = LocationRepository(db_session)
    state = SceneStateService(db_session)

    await campaigns.create(campaign_id, CampaignCreate(name="Route-scoped identity"))
    source = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Площадь"),
    )
    pier_seven = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Причал №7"),
    )
    warehouse_seven = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Склад №7"),
    )
    await state.create_exit(
        campaign_id,
        source.id,
        LocationExitCreate(
            to_location_id=pier_seven.id,
            label="Причал номер семь",
            bidirectional=True,
        ),
    )

    executor = SceneTransitionExecutor(db_session)
    resolved = await executor._resolve_existing_location(
        campaign_id,
        source.id,
        "Седьмой причал",
    )
    assert resolved is not None
    assert resolved.id == pier_seven.id

    unrelated = await executor._resolve_existing_location(
        campaign_id,
        source.id,
        "Седьмой склад",
    )
    assert unrelated is None or unrelated.id == warehouse_seven.id
    assert unrelated is None or unrelated.id != pier_seven.id
