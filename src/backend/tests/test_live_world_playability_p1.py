"""P1 regression contracts: physical objects, alternate NPC names, clock and meta leaks."""
from uuid import uuid4

import pytest

from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.location_repo import LocationRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.tables import Entity, Item
from app.models.addressed_response import AddressedResponse
from app.models.campaign import CampaignCreate, CampaignUpdate
from app.models.character import CharacterCreate
from app.models.location import LocationCreate
from app.models.scene import SceneCreate
from app.models.turn_authority import PlannedItemIntroduction, TurnAuthority
from app.services.narration_meta_guard import leaked_meta_lines
from app.services.scene_lifecycle import SceneLifecycleService
from app.services.scene_state_service import SceneStateService
from app.services.turn_outcome_materializer import (
    MaterializedTurnOutcome,
    TurnOutcomeMaterializer,
)


async def _start(db_session):
    cid = uuid4()
    await CampaignRepository(db_session).create(cid, CampaignCreate(name="P1 live world"))
    room = await LocationRepository(db_session).create(
        cid, LocationCreate(canonical_name="Ночной архив")
    )
    hero = await EntityRepository(db_session).create_character(
        cid, CharacterCreate(canonical_name="Вера"),
    )
    await CampaignRepository(db_session).update(
        cid, CampaignUpdate(player_character_id=hero.id),
    )
    scene = await SceneRepository(db_session).create(
        cid, SceneCreate(title="Ночной архив", location_id=room.id),
    )
    await SceneLifecycleService(db_session).activate(cid, scene.id)
    return cid, room, hero, scene


@pytest.mark.asyncio
async def test_confirmed_item_survives_next_scene_context_and_rollback(db_session):
    cid, _room, hero, scene = await _start(db_session)
    authority = TurnAuthority(
        campaign_id=cid,
        trigger_turn_id=uuid4(),
        player_character_id=hero.id,
        target_scene_id=scene.id,
        player_input="Беру найденный ключ.",
        observable_consequences=["Вера получила ключ Олава."],
        allowed_new_items=[PlannedItemIntroduction(
            canonical_name="ключ Олава",
            evidence="Вера получила ключ Олава.",
            owned_by_player=True,
        )],
    )
    source_turn_id = uuid4()
    materializer = TurnOutcomeMaterializer(db_session)
    outcome = await materializer.materialize(authority, source_turn_id=source_turn_id)
    assert len(outcome.introduced_item_ids) == 1
    item_id = outcome.introduced_item_ids[0]
    row = await db_session.get(Item, str(item_id))
    assert row.current_owner_id == str(hero.id)
    assert row.current_location_id is None

    # The *next turn* reads items in the player's hands, not just on the ground.
    state = await SceneStateService(db_session).get(cid, scene.id)
    assert "ключ Олава" in state.object_names

    # Undo/regenerate must not duplicate this newly introduced physical entity.
    await materializer.rollback(outcome)
    await db_session.flush()
    assert await db_session.get(Item, str(item_id)) is None
    assert await db_session.get(Entity, str(item_id)) is None


@pytest.mark.asyncio
async def test_confirmed_alternate_npc_name_is_alias_not_duplicate(db_session):
    cid, _room, hero, scene = await _start(db_session)
    keeper = await EntityRepository(db_session).create_character(
        cid, CharacterCreate(canonical_name="Привратник Ночного рынка")
    )
    authority = TurnAuthority(
        campaign_id=cid, trigger_turn_id=uuid4(), player_character_id=hero.id,
        player_input="Как тебя зовут?", target_scene_id=scene.id,
        addressed_response=AddressedResponse(
            speaker_id=keeper.id, speaker_name="Привратник Ночного рынка",
            revealed_name="Настя Круг", name_evidence="Меня зовут Настя Круг.",
        ),
    )
    materializer = TurnOutcomeMaterializer(db_session)
    assert await materializer._reveal_identity(authority, uuid4()) is None
    delta = await materializer._reveal_identity(
        authority, uuid4(), confirmed_by_published_beat=True,
    )
    assert delta is not None
    updated = await EntityRepository(db_session).get_by_id(keeper.id)
    assert updated.canonical_name == "Привратник Ночного рынка"
    assert "Настя Круг" in updated.aliases
    assert await materializer._reveal_identity(
        authority, uuid4(), confirmed_by_published_beat=True
    ) is None
    npcs = await EntityRepository(db_session).list_by_campaign(cid, entity_type="character")
    assert len(npcs) == 2  # Player + one (aliased) gatekeeper.
    await materializer.rollback(MaterializedTurnOutcome(identity_updates=(delta,)))
    restored = await EntityRepository(db_session).get_by_id(keeper.id)
    assert "Настя Круг" not in restored.aliases


@pytest.mark.asyncio
async def test_world_clock_advances_only_after_published_ticks(db_session):
    cid, _room, _hero, scene = await _start(db_session)
    service = SceneStateService(db_session)
    runtime = await service.ensure_runtime_state(scene.id)
    runtime.world_time_label = "night"
    runtime.world_time_order = 0
    await db_session.flush()

    for _ in range(service.TURNS_PER_PHASE - 1):
        assert await service.advance_published_turn(scene.id) is None
    assert runtime.world_time_label == "night"
    assert await service.advance_published_turn(scene.id) == "dawn"
    assert runtime.world_time_label == "dawn"
    assert (await service.get(cid, scene.id)).world_time_label == "dawn"


def test_narrator_meta_guard_rejects_scratchpad_but_not_game_dialogue():
    assert leaked_meta_lines("Иара? Need no invented. End actionable.")
    assert leaked_meta_lines("Analysis: avoid introducing another NPC")
    assert leaked_meta_lines("[APPROVED TURN PLAN]")
    assert not leaked_meta_lines("— Это дело ещё не раскрыто, — сказала Иара.")
    assert not leaked_meta_lines("Она долго смотрит на неосвещённые окна.")
