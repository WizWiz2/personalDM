"""QA-301: physical receipts, noncontradictory checks and durable conversation continuity."""
import asyncio
import json
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.config import settings
from app.db.repositories.belief_repo import BeliefRepository
from app.db.repositories.turn_repo import TurnRepository
from app.db.tables import Character, Entity, GenerationRun, Item, Location, Turn
from app.models.item_state import ItemContentRevelation, ItemPositionClaim, ItemStateSnapshot
from app.models.narration_validation import GrantedBeat
from app.models.player_intent import ActionOutcomeDecision, PlayerActionIntent, PlayerIntentContract, TurnOutcomeDecision
from app.models.turn import TurnCreate
from app.models.turn_authority import PlannedItemIntroduction, TurnAuthority, WorldThreadClosure
from app.services.action_plan_compiler import ActionPlanCompiler
from app.services.canon_semantics import canon_envelope_for_entities
from app.services.item_state_authority import item_position_violations
from app.services.player_intent_interpreter import PlayerActionIntentDraft, PlayerIntentContractDraft, normalize_intent_draft
from app.services.published_speech_memory import remember_published_speech
from app.services.turn_authority_resolvers import NpcIntroductionResolver
from app.services.turn_authority_validator import TurnAuthorityValidator
from app.services.turn_outcome_resolver import _outcome_wire_model
from app.services.turn_planner import PlanContractError
from app.services.turn_undo_service import TurnUndoService
from app.services.world_momentum_service import WorldMomentumService
from tests.test_qa219_regressions import _world
from tests.test_qa223_regressions import _execute


def success(index=0, text="Действие выполнено."):
    return ActionOutcomeDecision(action_index=index, resolution="auto_success", observable_outcome=text)


@pytest.mark.asyncio
async def test_successful_read_exposes_and_preserves_the_exact_registered_document(db_session):
    cid, _, _, _, _, letter, scene = await _world(db_session)
    contract = PlayerIntentContract(summary="Попросить прочесть адресата.", actions=[PlayerActionIntent(
        action_type="service", intent="Прочесть вслух адресата документа.", subject_item_id=letter,
        expected_effect="item_read")])
    missing = TurnOutcomeDecision(action_outcomes=[success(text="Оператор оглашает реквизиты.")])
    with pytest.raises(PlanContractError, match="Successful reading"):
        await ActionPlanCompiler(db_session).compile(cid, contract, missing)
    text = "Адресат: Вера Люнд. Доставить на береговой пост."
    decision = missing.model_copy(update={"item_revelations": [ItemContentRevelation(
        item_id=letter, action_index=0, content=text)]})
    first = await _execute(db_session, cid, scene, contract, decision)
    assert text in first.steps[0].observable_outcome
    # A reread cannot replace the canon with another document's content.
    wrong = missing.model_copy(update={"item_revelations": [ItemContentRevelation(
        item_id=letter, action_index=0, content="Другой адресат и другое распоряжение.")]})
    second = await _execute(db_session, cid, scene, contract, wrong)
    assert text in second.steps[0].observable_outcome
    assert "Другой адресат" not in second.steps[0].observable_outcome


def test_spoken_read_request_is_not_erased_by_information_only_flag():
    document = uuid4()
    draft = PlayerIntentContractDraft(summary="Попросить прочитать документ.", information_request_only=True,
        actions=[PlayerActionIntentDraft(action_type="service", intent="Прочтите документ вслух.",
            expected_effect="item_read", subject_item_id=str(document), actor_role="addressee")])
    assert normalize_intent_draft(draft, "Прочтите документ вслух.").actions[0].expected_effect == "item_read"


@pytest.mark.asyncio
async def test_new_note_has_its_own_id_and_never_transfers_or_overwrites_the_material(db_session):
    cid, room, _, hero, npc, material, scene = await _world(db_session)
    paper = await db_session.get(Item, str(material))
    paper.current_owner_id, paper.current_location_id = None, str(room)
    contract = PlayerIntentContract(summary="Изготовить записку из листа и отдать Алисе.", actions=[
        PlayerActionIntent(action_type="interaction", intent="Изготовить отдельную записку из листа стопки.",
                           expected_effect="item_creation", subject_item_id=material),
        PlayerActionIntent(action_type="inventory", intent="Отдать изготовленную записку Алисе.",
            produced_item_action_index=0, produced_item_ordinal=0, inventory_operation="give", inventory_target_id=npc),
    ])
    result = await _execute(db_session, cid, scene, contract, TurnOutcomeDecision(
        action_outcomes=[success(0), success(1)], item_introductions=[PlannedItemIntroduction(
            canonical_name="Записка инспектора", evidence="Изготовлена отдельная записка инспектора.",
            after_action_index=0, owned_by_player=True,
            written_content="Вечерняя сверка не завершена. Инспектор Р. Бауэр.")]))
    created = result.steps[1].item_id
    assert created != material
    from app.services.item_content_authority import revealed_item_contents
    contents = await revealed_item_contents(db_session, cid)
    assert contents[str(created)][0] == "Вечерняя сверка не завершена. Инспектор Р. Бауэр."
    assert str(material) not in contents
    assert (await db_session.get(Item, str(created))).current_owner_id == str(npc)
    assert paper.current_owner_id is None and paper.current_location_id == str(room)


def test_native_fact_schema_requires_the_registered_subject_before_generation():
    subject = str(uuid4())
    wire = canon_envelope_for_entities({subject})
    schema = wire.model_json_schema()
    fact_schema = next(value for value in schema["$defs"].values()
                       if value.get("properties", {}).get("change_type", {}).get("const") == "fact")
    assert {"subject_entity_id", "fact_type"} <= set(fact_schema["required"])
    proposal = {"outcome_id": "o", "change_type": "fact", "fact_type": "condition",
                "payload": {"subject": "Документ", "object_value": "цел"}}
    with pytest.raises(ValidationError):
        wire.model_validate({"proposals": [proposal]})
    wire.model_validate({"proposals": [{**proposal, "subject_entity_id": subject}]})


def test_validator_cannot_reverse_a_canonical_position_through_free_text_correction():
    document, room = uuid4(), uuid4()
    text = "Бланк с проколотым углом остаётся в деле."
    authority = TurnAuthority(campaign_id=uuid4(), trigger_turn_id=uuid4(), player_input="Проверяю бланк.",
        item_states=[ItemStateSnapshot(item_id=document, name="Бланк с проколотым углом", location_id=room,
                                      location_name="Зал станции")])
    result = TurnAuthorityValidator._interpret(authority, text, {
        "verdict": "repair_required", "violations": [{"violation_type": "canon_conflict", "severity": "error",
            "state_domain": "item_position", "item_id": str(document), "evidence": text,
            "correction": "Положить проколотый бланк под дверь маяка."}],
        "item_reviews": [{"item_id": str(document), "reference": "position", "claims": [{
            "item_id": str(document), "evidence": text, "scope": "current", "mode": "located",
            "location_id": str(room), "polarity": "asserted"}]}],
    })
    assert result.verdict == "pass" and not result.violations


@pytest.mark.parametrize("denied_room_is_current", [False, True])
def test_position_denials_are_checked_without_turning_them_into_positive_claims(denied_room_is_current):
    document, room, elsewhere = uuid4(), uuid4(), uuid4()
    text = "Проколотого бланка у маяка нет."
    state = ItemStateSnapshot(item_id=document, name="Проколотый бланк", location_id=room)
    claim = ItemPositionClaim(item_id=document, evidence=text, scope="current", mode="located",
        location_id=room if denied_room_is_current else elsewhere, polarity="denied")
    assert bool(item_position_violations([state], [claim], text)) == denied_room_is_current


@pytest.mark.asyncio
async def test_a_location_role_does_not_materialize_an_invisible_duplicate_operator(db_session):
    cid, room, _, _, _, _, _ = await _world(db_session)
    location = await db_session.get(Location, str(room))
    entity = await db_session.get(Entity, str(room))
    entity.custom_fields = json.dumps({"resident_role": "дежурный оператор станции"})
    assert location is not None
    result = await NpcIntroductionResolver(db_session).resolve(campaign_id=cid, introductions=[],
        present_names=["Вера", "Алиса"], target_location_id=room)
    assert not result.new_introductions
    assert result.present_names == ["Вера", "Алиса"]


@pytest.mark.asyncio
async def test_published_confirmation_survives_background_failure_and_later_undo(db_session):
    cid, _, _, hero, npc, _, scene = await _world(db_session)
    turns = TurnRepository(db_session)
    text = "Подтверждаю передачу без названного получателя."
    promise = "Я передам оригинал и отмечу подтверждение в журнале."
    user = await turns.create(cid, TurnCreate(role="user", content=text, scene_id=scene))
    assistant = await turns.create(cid, TurnCreate(role="assistant", content=f"«{promise}»", scene_id=scene,
        parent_turn_id=user.id))
    authority = TurnAuthority(campaign_id=cid, trigger_turn_id=user.id, player_input=text,
        player_character_id=hero, beat_owner_id=npc, beat_owner_name="Алиса")
    await remember_published_speech(db_session, authority, assistant, {"spoken_contributions": [text]},
        GrantedBeat(cast_id=str(npc), kind="speech", evidence=promise), [hero, npc])
    assert text in [item.proposition for item in await BeliefRepository(db_session).get_for_character(npc)]
    # No Scribe job runs. A later pair's undo rebuilds memory from accepted evidence.
    later = await turns.create(cid, TurnCreate(role="user", content="Смотрю в окно.", scene_id=scene))
    await turns.create(cid, TurnCreate(role="assistant", content="За окном туман.", scene_id=scene, parent_turn_id=later.id))
    await db_session.commit()
    assert await TurnUndoService(db_session).undo_last_pair(cid)
    assert text in [item.proposition for item in await BeliefRepository(db_session).get_for_character(npc)]
    assert promise in [item.proposition for item in await BeliefRepository(db_session).recall_spoken(npc, promise)]
    assert await TurnUndoService(db_session).undo_last_pair(cid)
    assert not await BeliefRepository(db_session).get_for_character(npc)


@pytest.mark.asyncio
async def test_old_promise_is_recalled_after_the_listener_window_is_full(db_session):
    from app.models.belief import BeliefCreate
    from app.services.base_context_compiler import ContextCompiler

    cid, _, _, hero, npc, _, scene = await _world(db_session)
    repo = BeliefRepository(db_session)
    promise = "Оригинал передам, подтверждение распределения отмечу в журнале."
    await repo.create(BeliefCreate(character_id=hero, source_character_id=npc, proposition=promise))
    for i in range(40):
        await repo.create(BeliefCreate(character_id=hero, source_character_id=npc,
                                      proposition=f"Очередная проверка окна номер {i}."))
    messages, _ = await ContextCompiler(db_session).compile_context(cid, scene_id=scene,
        current_user_content="Вы обещали отметить подтверждение распределения в журнале.")
    assert promise in messages[0].content


@pytest.mark.asyncio
async def test_return_presence_observation_ignores_legacy_descriptions(db_session):
    cid, _, elsewhere, hero, npc, _, _ = await _world(db_session)
    (await db_session.get(Character, str(npc))).current_location_id = str(elsewhere)
    await db_session.flush()
    contract = PlayerIntentContract(summary="Осматриваю, кто здесь.", actions=[PlayerActionIntent(
        action_type="observation", intent="Посмотреть, кто здесь.", expected_effect="presence_check")])
    plan = await ActionPlanCompiler(db_session).compile(cid, contract, TurnOutcomeDecision(
        action_outcomes=[success(text="Алиса сидит здесь, как в старом описании комнаты.")]))
    assert "Алиса" not in plan.action_sequence.steps[0].observable_outcome
    assert "Вера" in plan.action_sequence.steps[0].observable_outcome


def test_a_closed_goal_cannot_drive_another_world_move():
    source = "Выдать бланк после представления инспектора."
    evidence = "Инспектор назвал имя и должность."
    wire = _outcome_wire_model(0, world_threads={"thread_0": True}, world_kinds=("npc_action",),
        thread_sources={"thread_0": {"source": source}}, review_evidence=evidence, initiative_due=True)
    data = {"action_outcomes": [], "npc_introductions": [], "observable_consequences": ["В зале тихо."],
        "thread_reviews": {"thread_0": {"status": "resolved", "evidence": evidence}}}
    wire.model_validate(data)  # No fake pressure required after all candidates have closed.
    with pytest.raises(ValidationError, match="resolved"):
        wire.model_validate({**data, "world_move": {"thread_id": "thread_0", "kind": "npc_action",
            "outcome": "Оператор снова требует назвать имя и должность."}})


@pytest.mark.asyncio
async def test_thread_closure_is_undone_with_its_source_pair(db_session):
    cid, _, _, _, npc, _, scene = await _world(db_session)
    statement = "Выдать бланк после представления инспектора."
    character = await db_session.get(Character, str(npc))
    character.current_intentions = json.dumps([statement])
    turns = TurnRepository(db_session)
    user = await turns.create(cid, TurnCreate(role="user", content="Я инспектор.", scene_id=scene))
    closure = WorldThreadClosure(source=statement, actor_id=npc, status="resolved", evidence="Я инспектор.")
    await turns.create(cid, TurnCreate(role="assistant", content="Представление принято.", scene_id=scene,
        parent_turn_id=user.id, context_snapshot={"turn_authority": {"world_thread_closures": [closure.model_dump(mode="json")]}}))
    momentum = WorldMomentumService(db_session)
    assert statement not in [item["source"] for item in await momentum.threads(cid, scene)]
    await db_session.commit()
    assert await TurnUndoService(db_session).undo_last_pair(cid)
    assert statement in [item["source"] for item in await momentum.threads(cid, scene)]


@pytest.mark.asyncio
async def test_saga_timeout_marks_failed_instead_of_leaving_a_running_turn(db_session, monkeypatch):
    from app.services.turn_saga import TurnSaga, active_tasks

    cid, _, _, _, _, _, scene = await _world(db_session)
    saga = TurnSaga(db_session)
    entered = asyncio.Event()

    async def hang(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(settings, "TURN_TIMEOUT_SECONDS", 0.03)
    monkeypatch.setattr(saga._config_repo, "get_by_campaign_id", hang)
    output = [part async for part in saga.run_turn_stream(cid, TurnCreate(
        role="user", content="Проверяю письмо.", scene_id=scene))]
    assert entered.is_set() and "Generation failed" in "".join(output)
    run = (await db_session.execute(select(GenerationRun).where(GenerationRun.campaign_id == str(cid)))).scalar_one()
    assert run.status == "failed" and "wall-clock" in run.error
    assert str(cid) not in active_tasks
    assert (await db_session.get(Turn, run.user_turn_id)).status == "failed"
    assert not (await db_session.execute(select(Turn).where(Turn.campaign_id == str(cid), Turn.role == "assistant"))).scalars().all()


@pytest.mark.asyncio
async def test_timeout_after_inventory_prepare_restores_custody(tmp_path, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from app.db.engine import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'timeout.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False, autoflush=False)() as session:
            await _assert_prepared_timeout(session, monkeypatch)
    finally:
        await engine.dispose()


async def _assert_prepared_timeout(db_session, monkeypatch):
    from app.db.repositories.provider_config_repo import ProviderConfigRepository
    from app.models.provider_config import ProviderConfigCreate
    from app.models.player_intent import NpcItemHandoff
    from app.services.authority_narration_pipeline import AuthorityNarrationPipeline
    from app.services.turn_saga import TurnSaga

    cid, _, _, hero, npc, letter, scene = await _world(db_session)
    await ProviderConfigRepository(db_session).create_or_update(cid, ProviderConfigCreate(
        base_url="http://localhost:11434/v1", model_name="test", context_window=16000))
    plan = await ActionPlanCompiler(db_session).compile(cid, PlayerIntentContract(summary="Получить письмо.",
        actions=[PlayerActionIntent(action_type="inventory", intent="Получить письмо.", item_id=letter,
            inventory_operation="take")]), TurnOutcomeDecision(action_outcomes=[success()],
        npc_item_handoffs=[NpcItemHandoff(item_id=letter, giver_id=npc,
            evidence="Алиса передаёт письмо Вере.", after_action_index=0)]))
    saga = TurnSaga(db_session)
    monkeypatch.setattr(saga, "_plan", AsyncMock(return_value=(plan, {"status": "completed"})))
    entered = asyncio.Event()

    async def hung_narrator(*args, **kwargs):
        entered.set()
        assert (await db_session.get(Item, str(letter))).current_owner_id == str(hero)
        await asyncio.Event().wait()

    monkeypatch.setattr(AuthorityNarrationPipeline, "generate", hung_narrator)
    monkeypatch.setattr(settings, "TURN_TIMEOUT_SECONDS", 2.0)
    [part async for part in saga.run_turn_stream(cid, TurnCreate(role="user", content="Получить письмо.", scene_id=scene))]
    assert entered.is_set()
    assert (await db_session.get(Item, str(letter))).current_owner_id == str(npc)
    run = (await db_session.execute(select(GenerationRun).where(GenerationRun.campaign_id == str(cid)))).scalar_one()
    assert run.status == "failed" and "wall-clock" in run.error
