"""QA-210 regressions: structured spatial truth, reviewed legacy repair and stable NPC aliases."""
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.db.repositories.entity_repo import EntityRepository
from app.db.tables import Entity, Item, NpcIdentityAnchor, Turn
from app.models.addressed_response import AddressedResponse
from app.models.entity import EntityCreate, EntityType
from app.models.turn_authority import PlannedNpcIntroduction, TurnAuthority
from app.services.action_sequence_executor import ActionSequenceExecutor
from app.services.initial_world_state import InitialWorldStateService
from app.services.legacy_item_reconciliation import (
    LegacyItemReconciliation, LegacyItemReconciliationRequest,
)
from app.services.scene_state_service import SceneStateService
from app.services.entity_identity import identity_key
from app.services.turn_outcome_materializer import TurnOutcomeMaterializer
from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.location_repo import LocationRepository
from app.db.repositories.scene_repo import SceneRepository
from app.models.campaign import CampaignCreate, CampaignUpdate
from app.models.character import CharacterCreate
from app.models.location import LocationCreate
from app.models.scene import SceneCreate
from app.services.scene_lifecycle import SceneLifecycleService


async def _start(db_session):
    cid = uuid4()
    await CampaignRepository(db_session).create(
        cid, CampaignCreate(name="QA-210 final physical canon"),
    )
    from app.services.session_zero_service import SessionZeroService
    await SessionZeroService(db_session).ensure_setup(cid)
    room = await LocationRepository(db_session).create(
        cid, LocationCreate(canonical_name="Портовый склад"),
    )
    hero = await EntityRepository(db_session).create_character(
        cid, CharacterCreate(canonical_name="Вера"),
    )
    await CampaignRepository(db_session).update(
        cid, CampaignUpdate(player_character_id=hero.id),
    )
    scene = await SceneRepository(db_session).create(
        cid, SceneCreate(title="Портовый склад", location_id=room.id),
    )
    await SceneLifecycleService(db_session).activate(cid, scene.id)
    return cid, room, hero, scene


@pytest.mark.asyncio
async def test_placed_item_position_is_visible_and_compensated(db_session):
    campaign_id, room, hero, scene = await _start(db_session)
    entity = await EntityRepository(db_session).create(
        campaign_id,
        EntityCreate(entity_type=EntityType.ITEM, canonical_name="Жетон Яна"),
    )
    item = Item(entity_id=str(entity.id), current_owner_id=str(hero.id))
    db_session.add(item)
    await db_session.flush()
    engine = ActionSequenceExecutor(db_session)
    receipt = SimpleNamespace()
    step = SimpleNamespace(
        item_id=entity.id, inventory_operation="place",
        inventory_target_id=None, item_placement_detail="под полотном на полке",
    )
    await engine._apply_inventory_step(campaign_id, scene.id, uuid4(), step, receipt)
    assert item.current_owner_id is None
    assert item.current_location_id == str(room.id)
    assert item.placement_detail == "под полотном на полке"
    state = await SceneStateService(db_session).get(campaign_id, scene.id)
    assert state.object_placements[str(entity.id)] == "под полотном на полке"
    assert "под полотном на полке" in SceneStateService.prompt_contract(state)
    # Undo restores BOTH global custody and fine-grained placement.
    item.current_owner_id = receipt.item_previous_owner_id
    item.current_location_id = receipt.item_previous_location_id
    item.placement_detail = receipt.item_previous_placement_detail
    assert item.current_owner_id == str(hero.id)
    assert item.placement_detail is None


@pytest.mark.asyncio
async def test_replay_snapshot_preserves_position_and_later_introduced_item(db_session):
    cid, room, hero, scene = await _start(db_session)
    entities = EntityRepository(db_session)
    e = await entities.create(
        cid, EntityCreate(entity_type=EntityType.ITEM, canonical_name="Старая печать"),
    )
    db_session.add(Item(
        entity_id=str(e.id), current_location_id=str(room.id),
        placement_detail="за книгами",
    ))
    await db_session.flush()
    checkpoint = InitialWorldStateService(db_session)
    await checkpoint.ensure_snapshot(cid)
    known = await db_session.get(Item, str(e.id))
    known.placement_detail = "на столе"

    new = await entities.create(
        cid, EntityCreate(entity_type=EntityType.ITEM, canonical_name="Новый жетон"),
    )
    db_session.add(Item(
        entity_id=str(new.id), current_owner_id=str(hero.id),
    ))
    await db_session.flush()
    await checkpoint.restore(cid)
    assert known.placement_detail == "за книгами"
    assert (await db_session.get(Item, str(new.id))).current_owner_id == str(hero.id)


@pytest.mark.asyncio
async def test_legacy_item_repair_requires_published_quote_and_explicit_approval(db_session):
    cid, room, hero, scene = await _start(db_session)
    turn_id = uuid4()
    db_session.add(Turn(
        id=str(turn_id), campaign_id=str(cid), role="assistant",
        content="Жетон Яна всё ещё лежит на его одежде.",
    ))
    await db_session.flush()
    repair = LegacyItemReconciliation(db_session)
    request = LegacyItemReconciliationRequest(
        canonical_name="Жетон Яна", source_turn_id=turn_id,
        published_evidence="Жетон Яна всё ещё лежит на его одежде.",
        current_location_id=room.id, placement_detail="на одежде Яна",
    )
    preview = await repair.reconcile(cid, request)
    assert preview["status"] == "preview"
    assert not await EntityRepository(db_session).list_by_campaign(cid, "item")
    created = await repair.reconcile(cid, request.model_copy(update={"approve": True}))
    assert created["status"] == "created"
    assert (await db_session.get(Item, created["item_id"])).placement_detail == "на одежде Яна"
    again = await repair.reconcile(cid, request.model_copy(update={"approve": True}))
    assert again["status"] == "already_exists"
    with pytest.raises(ValueError, match="different custody"):
        await repair.reconcile(cid, request.model_copy(update={
            "current_location_id": None, "current_owner_id": hero.id,
            "placement_detail": None, "approve": True,
        }))
    with pytest.raises(ValueError, match="verbatim"):
        await repair.reconcile(cid, request.model_copy(update={
            "published_evidence": "Искусственно придуманная реплика.",
        }))


@pytest.mark.asyncio
async def test_rename_and_undo_reuses_same_npc_id(db_session):
    cid, room, hero, scene = await _start(db_session)
    materializer = TurnOutcomeMaterializer(db_session)

    def intro(name):
        return TurnAuthority(
            campaign_id=cid, trigger_turn_id=uuid4(),
            player_character_id=hero.id, target_scene_id=scene.id,
            player_input="Позвать сторожа.",
            allowed_new_npcs=[
                PlannedNpcIntroduction(
                    canonical_name=name, role="сторож", temporary_name=True,
                    reason="Сторож вошёл в комнату.",
                ),
            ],
        )

    first = intro("Ночной сторож")
    source_turn_id = uuid4()
    outcome = await materializer.materialize(first, source_turn_id=source_turn_id)
    original_id = outcome.introduced_character_ids[0]
    first.addressed_response = AddressedResponse(
        speaker_id=original_id, speaker_name="Ночной сторож",
        revealed_name="Гус Фонарь", name_evidence="Меня зовут Гус Фонарь.",
    )
    await materializer._reveal_identity(
        first, uuid4(), confirmed_by_published_beat=True,
    )
    assert await db_session.get(
        NpcIdentityAnchor, (str(cid), identity_key("Гус Фонарь")),
    ) is not None
    await materializer.rollback(source_turn_id)
    assert await db_session.get(Entity, str(original_id)) is None
    regenerated = await materializer.materialize(
        intro("Гус Фонарь"), source_turn_id=uuid4(),
    )
    assert regenerated.introduced_character_ids == (original_id,)


@pytest.mark.asyncio
async def test_reposition_within_room_changes_position_without_changing_custody(db_session):
    cid, room, hero, scene = await _start(db_session)
    entity = await EntityRepository(db_session).create(
        cid, EntityCreate(entity_type=EntityType.ITEM, canonical_name="Жетон Яна"),
    )
    db_session.add(Item(
        entity_id=str(entity.id),
        current_location_id=str(room.id),
        placement_detail="под полотном",
    ))
    await db_session.flush()
    executor = ActionSequenceExecutor(db_session)
    receipt = SimpleNamespace()
    step = SimpleNamespace(
        item_id=entity.id, inventory_operation="reposition",
        inventory_target_id=None, item_placement_detail="под подошвой героя",
    )
    await executor._apply_inventory_step(cid, scene.id, uuid4(), step, receipt)
    item = await db_session.get(Item, str(entity.id))
    assert item.current_owner_id is None
    assert item.current_location_id == str(room.id)
    assert item.placement_detail == "под подошвой героя"
    assert receipt.item_previous_placement_detail == "под полотном"
    assert receipt.item_result_placement_detail == "под подошвой героя"
    # Reverting the receipt restores the old position instead of teleported prose.
    item.placement_detail = receipt.item_previous_placement_detail
    assert item.placement_detail == "под полотном"


@pytest.mark.asyncio
async def test_gm_item_transfer_is_replayed_after_checkpoint_undo(db_session):
    from app.db.tables import ItemCustodyCommand
    from app.services.active_canon_replay import ActiveCanonReplayService

    cid, room, hero, scene = await _start(db_session)
    entity = await EntityRepository(db_session).create(
        cid, EntityCreate(entity_type=EntityType.ITEM, canonical_name="Жетон Яна"),
    )
    db_session.add(Item(
        entity_id=str(entity.id), current_location_id=str(room.id),
        placement_detail="на теле Яна",
    ))
    await db_session.flush()
    await InitialWorldStateService(db_session).ensure_snapshot(cid)
    item = await db_session.get(Item, str(entity.id))
    item.current_owner_id = str(hero.id)
    item.current_location_id = None
    item.placement_detail = None
    db_session.add(ItemCustodyCommand(
        campaign_id=str(cid), item_id=str(entity.id),
        owner_id=str(hero.id), location_id=None, placement_detail=None,
    ))
    await db_session.flush()
    assert item.current_owner_id == str(hero.id)
    await ActiveCanonReplayService(db_session).replay(cid)
    assert item.current_owner_id == str(hero.id)
    assert item.current_location_id is None
    assert item.placement_detail is None
