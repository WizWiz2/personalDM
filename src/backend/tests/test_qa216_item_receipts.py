"""F-44: an NPC handoff must have a real structured custody receipt."""
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.location_repo import LocationRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.tables import Item
from app.models.campaign import CampaignCreate, CampaignUpdate
from app.models.character import CharacterCreate
from app.models.entity import EntityCreate, EntityType
from app.models.location import LocationCreate
from app.models.scene import SceneCreate
from app.services.action_sequence_executor import ActionSequenceExecutor, InventoryExecutionError
from app.services.turn_planner import ActionStepPlan


async def setup_handoff(db_session: AsyncSession):
    campaign_id = uuid4()
    campaigns = CampaignRepository(db_session)
    entities = EntityRepository(db_session)
    locations = LocationRepository(db_session)
    scenes = SceneRepository(db_session)
    await campaigns.create(campaign_id, CampaignCreate(name="F-44 receipt"))
    room = await locations.create(campaign_id, LocationCreate(canonical_name="Архив"))
    elsewhere = await locations.create(campaign_id, LocationCreate(canonical_name="Лавка"))
    hero = await entities.create_character(campaign_id, CharacterCreate(
        canonical_name="Вера", current_location_id=room.id,
    ))
    npc = await entities.create_character(campaign_id, CharacterCreate(
        canonical_name="Алиса", current_location_id=room.id,
    ))
    await campaigns.update(campaign_id, CampaignUpdate(player_character_id=hero.id))
    item = await entities.create(campaign_id, EntityCreate(
        entity_type=EntityType.ITEM, canonical_name="письмо Оста Эндлина",
    ))
    db_session.add(Item(entity_id=str(item.id), current_owner_id=str(npc.id)))
    scene = await scenes.create(campaign_id, SceneCreate(
        title="Архив", location_id=room.id,
    ))
    await db_session.flush()
    return campaign_id, room.id, elsewhere.id, hero.id, npc.id, item.id, scene.id


@pytest.mark.asyncio
async def test_npc_receives_to_player_only_with_authoritative_item_receipt(db_session):
    cid, _, _, hero, npc, item_id, scene_id = await setup_handoff(db_session)
    step = ActionStepPlan(
        action_type="interaction",
        intent="Алиса добровольно вручает письмо Вере",
        resolution="auto_success",
        observable_outcome="Алиса вручает письмо Вере.",
        item_id=item_id,
        inventory_operation="receive",
        inventory_target_id=npc,
    )
    assert step.action_type == "inventory"
    recorded = SimpleNamespace()
    await ActionSequenceExecutor(db_session)._apply_inventory_step(
        cid, scene_id, uuid4(), step, recorded,
    )
    item = await db_session.get(Item, str(item_id))
    assert item.current_owner_id == str(hero)
    assert item.current_location_id is None
    assert recorded.item_previous_owner_id == str(npc)
    assert recorded.item_result_owner_id == str(hero)
    assert recorded.item_operation == "receive"


@pytest.mark.asyncio
async def test_npc_absent_or_item_not_held_does_not_transfer(db_session):
    cid, room, elsewhere, hero, npc, item_id, scene_id = await setup_handoff(db_session)
    from app.db.tables import Character
    giver = await db_session.get(Character, str(npc))
    giver.current_location_id = str(elsewhere)
    step = ActionStepPlan(
        action_type="inventory", intent="Получаю письмо от отсутствующей Алисы",
        resolution="auto_success", item_id=item_id,
        inventory_operation="receive", inventory_target_id=npc,
        observable_outcome="Письмо у Веры",
    )
    with pytest.raises(InventoryExecutionError, match="physically present"):
        await ActionSequenceExecutor(db_session)._apply_inventory_step(
            cid, scene_id, uuid4(), step, SimpleNamespace(),
        )
    item = await db_session.get(Item, str(item_id))
    assert item.current_owner_id == str(npc)
    giver.current_location_id = str(room)
    item.current_owner_id = str(hero)
    with pytest.raises(InventoryExecutionError, match="not owned"):
        await ActionSequenceExecutor(db_session)._apply_inventory_step(
            cid, scene_id, uuid4(), step, SimpleNamespace(),
        )


@pytest.mark.asyncio
async def test_outcome_compiler_adds_npc_receipt_after_successful_service():
    from unittest.mock import AsyncMock
    from app.models.player_intent import (
        ActionOutcomeDecision, NpcItemHandoff, PlayerActionIntent,
        PlayerIntentContract, TurnOutcomeDecision,
    )
    from app.services.action_plan_compiler import ActionPlanCompiler

    giver_id, item_id = uuid4(), uuid4()
    compiler = ActionPlanCompiler.__new__(ActionPlanCompiler)
    compiler._world = AsyncMock(return_value=(
        None, SimpleNamespace(location_id=uuid4()), []
    ))
    contract = PlayerIntentContract(
        summary="Прошу Алису отдать письмо",
        actions=[PlayerActionIntent(action_type="service", intent="Прошу Алису отдать письмо")],
    )
    decision = TurnOutcomeDecision(
        action_outcomes=[ActionOutcomeDecision(
            action_index=0, resolution="auto_success",
            observable_outcome="Алиса вручает письмо Вере.",
        )],
        npc_item_handoffs=[NpcItemHandoff(
            item_id=item_id, giver_id=giver_id,
            evidence="Алиса вручает письмо Вере.",
            after_action_index=0,
        )],
    )
    plan = await compiler.compile(uuid4(), contract, decision)
    assert len(plan.action_sequence.steps) == 2
    receipt = plan.action_sequence.steps[-1]
    assert receipt.action_type == "inventory"
    assert receipt.inventory_operation == "receive"
    assert receipt.item_id == item_id
    assert receipt.inventory_target_id == giver_id


@pytest.mark.asyncio
async def test_outcome_compiler_rejects_unconfirmed_npc_handoff():
    from unittest.mock import AsyncMock
    from app.models.player_intent import (
        ActionOutcomeDecision, NpcItemHandoff, PlayerActionIntent,
        PlayerIntentContract, TurnOutcomeDecision,
    )
    from app.services.action_plan_compiler import ActionPlanCompiler
    from app.services.turn_planner import TurnPlanningError

    giver, item = uuid4(), uuid4()
    compiler = ActionPlanCompiler.__new__(ActionPlanCompiler)
    compiler._world = AsyncMock(return_value=(
        None, SimpleNamespace(location_id=uuid4()), []
    ))
    contract = PlayerIntentContract(
        summary="Прошу письмо", actions=[PlayerActionIntent(
            action_type="service", intent="Прошу письмо",
        )],
    )
    decision = TurnOutcomeDecision(
        action_outcomes=[ActionOutcomeDecision(
            action_index=0, resolution="auto_success",
            observable_outcome="Алиса отказывает в передаче.",
        )],
        npc_item_handoffs=[NpcItemHandoff(
            item_id=item, giver_id=giver,
            evidence="Алиса вручает письмо.", after_action_index=0,
        )],
    )
    with pytest.raises(TurnPlanningError, match="lacks an approved"):
        await compiler.compile(uuid4(), contract, decision)
