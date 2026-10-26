from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.turn_repo import TurnRepository
from app.db.tables import Item, ItemContentDisclosure
from app.models.entity import EntityCreate, EntityType
from app.models.item_state import ItemContentRevelation
from app.models.player_intent import (
    ActionOutcomeDecision,
    NpcItemHandoff,
    PlayerActionIntent,
    PlayerIntentContract,
    TurnOutcomeDecision,
)
from app.models.turn import TurnCreate
from app.models.turn_authority import PlannedItemIntroduction
from app.services.action_plan_compiler import ActionPlanCompiler
from app.services.action_sequence_executor import ActionSequenceExecutor
from app.services.item_state_authority import load_item_states
from app.services.turn_authority_service import TurnAuthorityService
from app.services.turn_outcome_materializer import TurnOutcomeMaterializer
from app.services.turn_outcome_resolver import _outcome_wire_model
from app.services.turn_planner import TurnPlanningError
from tests.test_qa219_regressions import _world

pytestmark = pytest.mark.interagent_contract_enforced


async def _read(session, cid, scene, item, content):
    text = "Читаю документ."
    contract = PlayerIntentContract(summary=text, actions=[PlayerActionIntent(action_type="observation", intent=text, subject_item_id=item)])
    decision = TurnOutcomeDecision(action_outcomes=[ActionOutcomeDecision(action_index=0,
        resolution="auto_success", observable_outcome="В документе обнаружен текст.")],
        item_revelations=[ItemContentRevelation(item_id=item, action_index=0, content=content)])
    plan = await ActionPlanCompiler(session).compile(cid, contract, decision, player_input=text)
    user = await TurnRepository(session).create(cid, TurnCreate(role="user", content=text, scene_id=scene))
    execution = await ActionSequenceExecutor(session).execute(cid, scene, user.id, plan.action_sequence)
    return execution


@pytest.mark.asyncio
async def test_revealed_content_survives_session_reload_and_cannot_be_rewritten(db_session):
    cid, _, _, _, _, letter, scene = await _world(db_session)
    first = await _read(db_session, cid, scene, letter, "Не отдавайте ключ первому, кто назовёт ваше имя.")
    await ActionSequenceExecutor(db_session).mark_applied(first.sequence_id)
    await db_session.commit()
    db_session.expire_all()
    second = await _read(db_session, cid, scene, letter, "Приходите в другую комнату после третьего удара.")
    states = await load_item_states(db_session, cid)
    assert states[0].revealed_content == "Не отдавайте ключ первому, кто назовёт ваше имя."
    assert states[0].revealed_content in second.steps[0].observable_outcome
    assert "другую комнату" not in second.steps[0].observable_outcome
    assert len((await db_session.execute(select(ItemContentDisclosure))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_two_letters_do_not_exchange_content_and_repeated_undo_keeps_first_disclosure(db_session):
    cid, _, _, hero, _, letter, scene = await _world(db_session)
    other = await EntityRepository(db_session).create(cid, EntityCreate(
        entity_type=EntityType.ITEM, canonical_name="Второй документ"))
    db_session.add(Item(entity_id=str(other.id), current_owner_id=str(hero)))
    await db_session.flush()
    first = await _read(db_session, cid, scene, letter, "Первый текст: не отдавать ключ.")
    await ActionSequenceExecutor(db_session).mark_applied(first.sequence_id)
    await _read(db_session, cid, scene, other.id, "Второй текст: встретиться у причала.")
    reread = await _read(db_session, cid, scene, letter, "Второй текст: встретиться у причала.")
    await ActionSequenceExecutor(db_session).rollback_prepared(reread.sequence_id)
    states = {s.item_id: s for s in await load_item_states(db_session, cid)}
    assert states[letter].revealed_content == "Первый текст: не отдавать ключ."
    assert states[other.id].revealed_content == "Второй текст: встретиться у причала."
    await ActionSequenceExecutor(db_session).undo_applied(first.sequence_id)
    states = {s.item_id: s for s in await load_item_states(db_session, cid)}
    assert states[letter].revealed_content is None
    assert states[other.id].revealed_content is not None


@pytest.mark.asyncio
async def test_failed_publication_rolls_back_a_first_discovery(db_session):
    cid, _, _, _, _, letter, scene = await _world(db_session)
    execution = await _read(db_session, cid, scene, letter, "Раскрытая записка.")
    assert (await load_item_states(db_session, cid))[0].revealed_content is not None
    await ActionSequenceExecutor(db_session).rollback_prepared(execution.sequence_id)
    assert (await load_item_states(db_session, cid))[0].revealed_content is None


@pytest.mark.asyncio
async def test_blocked_source_action_cannot_disclose_an_item(db_session):
    cid, _, _, _, _, letter, _ = await _world(db_session)
    contract = PlayerIntentContract(summary="Читаю.", actions=[PlayerActionIntent(action_type="observation", intent="Читаю.")])
    decision = TurnOutcomeDecision(action_outcomes=[ActionOutcomeDecision(action_index=0, resolution="blocked", blocking_reason="Документ недоступен.")],
        item_revelations=[ItemContentRevelation(item_id=letter, action_index=0, content="Ложное раскрытие.")])
    with pytest.raises(TurnPlanningError, match="successful reading"):
        await ActionPlanCompiler(db_session).compile(cid, contract, decision)
    assert (await load_item_states(db_session, cid))[0].revealed_content is None


@pytest.mark.asyncio
async def test_handoff_does_not_require_duplicate_prose_to_be_identical(db_session):
    cid, _, _, hero, npc, letter, scene = await _world(db_session)
    text = "Алиса, я за своей почтой."
    contract = PlayerIntentContract(summary=text, actions=[PlayerActionIntent(action_type="service", intent=text)])
    decision = TurnOutcomeDecision(action_outcomes=[ActionOutcomeDecision(action_index=0,
        resolution="auto_success", observable_outcome="Хозяйка соглашается вернуть почту адресату.")],
        npc_item_handoffs=[NpcItemHandoff(item_id=letter, giver_id=npc, after_action_index=0,
                                       evidence="Алиса протягивает Вере письмо Оста Эндлина.")])
    plan = await ActionPlanCompiler(db_session).compile(cid, contract, decision, player_input=text)
    user = await TurnRepository(db_session).create(cid, TurnCreate(role="user", content=text, scene_id=scene))
    report = await ActionSequenceExecutor(db_session).execute(cid, scene, user.id, plan.action_sequence)
    assert report.steps[-1].status == "completed" and report.steps[-1].item_operation == "receive"
    assert (await db_session.get(Item, str(letter))).current_owner_id == str(hero)


@pytest.mark.asyncio
async def test_handoff_still_requires_a_completed_prerequisite(db_session):
    cid, _, _, _, npc, letter, _ = await _world(db_session)
    contract = PlayerIntentContract(summary="Попросить.", actions=[PlayerActionIntent(action_type="service", intent="Попросить.")])
    decision = TurnOutcomeDecision(action_outcomes=[ActionOutcomeDecision(action_index=0, resolution="blocked", blocking_reason="Условие не выполнено.")],
        npc_item_handoffs=[NpcItemHandoff(item_id=letter, giver_id=npc, after_action_index=0, evidence="Передача заявлена преждевременно.")])
    with pytest.raises(TurnPlanningError, match="unfinished action"):
        await ActionPlanCompiler(db_session).compile(cid, contract, decision)


@pytest.mark.asyncio
async def test_created_artifact_is_materialized_by_typed_source_not_matching_words(db_session):
    cid, _, _, hero, _, original, scene = await _world(db_session)
    await _read(db_session, cid, scene, original, "Исходный текст с неизменной подписью.")
    text = "Делаю копию записки."
    contract = PlayerIntentContract(summary=text, actions=[PlayerActionIntent(action_type="interaction", intent=text)])
    decision = TurnOutcomeDecision(action_outcomes=[ActionOutcomeDecision(action_index=0, resolution="auto_success",
        observable_outcome="Записка переписана на отдельный лист, лист остаётся у героя.")],
        item_introductions=[PlannedItemIntroduction(canonical_name="Переписанная записка", evidence="Герой изготовил отдельный экземпляр.",
            owned_by_player=True, after_action_index=0, copy_of_item_id=original)])
    plan = await ActionPlanCompiler(db_session).compile(cid, contract, decision, player_input=text)
    user = await TurnRepository(db_session).create(cid, TurnCreate(role="user", content=text, scene_id=scene))
    report = await ActionSequenceExecutor(db_session).execute(cid, scene, user.id, plan.action_sequence)
    plan.scene_transition.execution_report = report.model_dump(mode="json")
    authority = await TurnAuthorityService(db_session).build(campaign_id=cid, trigger_turn_id=user.id,
        player_input=text, source_scene_id=scene, target_scene_id=scene, plan=plan, acting_character_id=None)
    outcome = await TurnOutcomeMaterializer(db_session).materialize(authority, source_turn_id=user.id)
    assert len(outcome.introduced_item_ids) == 1
    item = await db_session.get(Item, str(outcome.introduced_item_ids[0]))
    assert item.current_owner_id == str(hero)
    states = {s.item_id: s for s in await load_item_states(db_session, cid)}
    assert states[outcome.introduced_item_ids[0]].revealed_content == states[original].revealed_content


@pytest.mark.asyncio
async def test_repeated_readings_in_one_turn_share_one_disclosure(db_session):
    cid, _, _, _, _, letter, scene = await _world(db_session)
    contract = PlayerIntentContract(summary="Читаю, затем перечитываю.", actions=[
        PlayerActionIntent(action_type="observation", intent="Читаю.", subject_item_id=letter),
        PlayerActionIntent(action_type="observation", intent="Перечитываю.", subject_item_id=letter),
    ])
    decision = TurnOutcomeDecision(action_outcomes=[ActionOutcomeDecision(action_index=i, resolution="auto_success", observable_outcome="Прочитано.") for i in range(2)],
        item_revelations=[ItemContentRevelation(item_id=letter, action_index=0, content="Первое раскрытие."),
                          ItemContentRevelation(item_id=letter, action_index=1, content="Подмена в том же ходе.")])
    plan = await ActionPlanCompiler(db_session).compile(cid, contract, decision)
    assert all("Первое раскрытие." in step.observable_outcome for step in plan.action_sequence.steps)
    user = await TurnRepository(db_session).create(cid, TurnCreate(role="user", content=contract.summary, scene_id=scene))
    await ActionSequenceExecutor(db_session).execute(cid, scene, user.id, plan.action_sequence)
    assert len((await db_session.execute(select(ItemContentDisclosure))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_one_reading_act_can_disclose_two_distinct_documents(db_session):
    cid, _, _, hero, _, letter, scene = await _world(db_session)
    other = await EntityRepository(db_session).create(cid, EntityCreate(entity_type=EntityType.ITEM, canonical_name="Другая записка"))
    db_session.add(Item(entity_id=str(other.id), current_owner_id=str(hero)))
    await db_session.flush()
    contract = PlayerIntentContract(summary="Читаю оба документа.", actions=[PlayerActionIntent(action_type="observation", intent="Читаю оба документа.")])
    decision = TurnOutcomeDecision(action_outcomes=[ActionOutcomeDecision(action_index=0, resolution="auto_success", observable_outcome="Оба документа прочитаны.")],
        item_revelations=[ItemContentRevelation(item_id=letter, action_index=0, content="Первое содержание."),
                          ItemContentRevelation(item_id=other.id, action_index=0, content="Второе содержание.")])
    plan = await ActionPlanCompiler(db_session).compile(cid, contract, decision)
    user = await TurnRepository(db_session).create(cid, TurnCreate(role="user", content=contract.summary, scene_id=scene))
    await ActionSequenceExecutor(db_session).execute(cid, scene, user.id, plan.action_sequence)
    states = {s.item_id: s for s in await load_item_states(db_session, cid)}
    assert states[letter].revealed_content == "Первое содержание."
    assert states[other.id].revealed_content == "Второе содержание."
    assert states[letter].content_source_step_id == states[other.id].content_source_step_id


def test_native_boundary_cannot_approve_handoff_against_typed_refusal():
    from uuid import uuid4
    wire = _outcome_wire_model(1)
    with pytest.raises(ValidationError, match="typed refusal"):
        wire.model_validate({"npc_introductions": [], "action_outcomes": [{"action_index": 0, "resolution": "auto_success", "npc_response": "refused",
            "observable_outcome": "NPC отказывается."}], "npc_item_handoffs": [{"item_id": str(uuid4()), "giver_id": str(uuid4()),
            "after_action_index": 0, "evidence": "Передача якобы состоялась."}]})


def test_native_boundary_cannot_exchange_the_frozen_document_subject():
    from uuid import uuid4
    target, other = uuid4(), uuid4()
    wire = _outcome_wire_model(1, discovery_targets={0: str(target)})
    with pytest.raises(ValidationError, match="frozen subject"):
        wire.model_validate({"npc_introductions": [], "action_outcomes": [{"action_index": 0, "resolution": "auto_success", "observable_outcome": "Документ прочитан."}],
            "item_revelations": [{"item_id": str(other), "action_index": 0, "content": "Подменённое содержание."}]})
