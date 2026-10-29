"""QA-300: distinct artifacts, current occupants and bounded scene memory."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.tables import Character, Item
from app.models.player_intent import (
    ActionOutcomeDecision,
    PlayerActionIntent,
    PlayerIntentContract,
    TurnOutcomeDecision,
)
from app.models.turn import ChatMessage
from app.models.turn_authority import PlannedItemIntroduction
from app.services.base_context_compiler import ContextCompiler
from app.services.player_intent_interpreter import _intent_wire_model
from tests.test_qa219_regressions import _world
from tests.test_qa223_regressions import _execute


@pytest.mark.asyncio
async def test_two_copies_from_one_creation_can_be_given_and_placed_separately(db_session):
    cid, room, _, _, npc, _, scene = await _world(db_session)
    contract = PlayerIntentContract(summary="Сделать два оттиска, первый отдать, второй оставить на столе.", actions=[
        PlayerActionIntent(action_type="interaction", intent="Сделать два отдельных оттиска.", expected_effect="item_creation"),
        PlayerActionIntent(action_type="inventory", intent="Отдать первый оттиск.", produced_item_action_index=0,
                           produced_item_ordinal=0, inventory_operation="give", inventory_target_id=npc),
        PlayerActionIntent(action_type="inventory", intent="Положить второй оттиск на стол.", produced_item_action_index=0,
                           produced_item_ordinal=1, inventory_operation="place", item_placement_detail="на столе"),
    ])
    decision = TurnOutcomeDecision(action_outcomes=[ActionOutcomeDecision(action_index=i, resolution="auto_success",
        observable_outcome="Действие выполнено.") for i in range(3)], item_introductions=[
        PlannedItemIntroduction(canonical_name=f"Оттиск {i+1}", after_action_index=0,
                                evidence="Изготовлены два отдельных оттиска.", owned_by_player=True)
        for i in range(2)
    ])
    execution = await _execute(db_session, cid, scene, contract, decision)
    assert execution.completed_steps == 3
    first, second = execution.steps[1].item_id, execution.steps[2].item_id
    assert first != second
    assert (await db_session.get(Item, str(first))).current_owner_id == str(npc)
    placed = await db_session.get(Item, str(second))
    assert placed.current_owner_id is None and placed.current_location_id == str(room)
    assert placed.placement_detail == "на столе"


def test_native_intent_rejects_a_previous_turn_artifact_reference():
    from uuid import uuid4
    hero, npc, item = map(str, (uuid4(), uuid4(), uuid4()))
    wire = _intent_wire_model([ChatMessage(role="system", content=(
        f"Controlled character: {hero}\nPhysically present characters: Герой [id={hero}], Алиса [id={npc}]\n"
        f"Player-owned items: Оттиск [id={item}]"))])
    with pytest.raises(ValidationError, match="earlier creation"):
        wire.model_validate({"summary": "Отдать вчерашний оттиск.", "actions": [{
            "evidence_quote": "Отдать вчерашний оттиск.", "contribution_kind": "world_action", "actor_id": hero,
            "action_type": "inventory", "intent": "Отдать оттиск.", "produced_item_action_index": 0,
            "produced_item_ordinal": 0, "inventory_operation": "give", "inventory_target_id": npc}]})


def test_native_created_artifact_reference_requires_an_explicit_ordinal():
    from app.services.player_intent_interpreter import _ProducedInventoryWire

    assert "produced_item_ordinal" in _ProducedInventoryWire.model_json_schema()["required"]


@pytest.mark.asyncio
async def test_context_cast_excludes_a_departed_historical_participant(db_session):
    _, _, elsewhere, _, npc, _, scene = await _world(db_session)
    character = await db_session.get(Character, str(npc))
    character.current_location_id = str(elsewhere)
    await db_session.flush()
    assert npc not in {person.id for person in await EntityRepository(db_session).get_characters_in_scene(scene)}
    # Historical scene membership remains available for replay and undo.
    assert npc in await SceneRepository(db_session).get_participants(scene)


def test_local_fact_window_is_bounded_after_hundreds_of_turns():
    from uuid import uuid4
    scene = uuid4()
    start = datetime(2026, 1, 1, tzinfo=UTC)
    facts = [SimpleNamespace(id=uuid4(), scene_id=scene, subject_entity_id=None,
        updated_at=start + timedelta(minutes=i)) for i in range(1000)]
    window = ContextCompiler._scene_window(facts, scene, set())
    assert len(window) <= ContextCompiler.SCENE_WINDOW_LOCAL_FACTS
    assert facts[-1] in window


@pytest.mark.asyncio
async def test_previous_turn_document_is_given_by_its_persisted_identity(db_session):
    cid, _, _, hero, npc, _, scene = await _world(db_session)
    created = await _execute(db_session, cid, scene, PlayerIntentContract(summary="Написать записку.", actions=[
        PlayerActionIntent(action_type="interaction", intent="Написать отдельную записку.", expected_effect="item_creation")
    ]), TurnOutcomeDecision(action_outcomes=[ActionOutcomeDecision(action_index=0, resolution="auto_success",
        observable_outcome="Записка написана.")], item_introductions=[PlannedItemIntroduction(
            canonical_name="Новая записка", after_action_index=0, evidence="Записка написана.", owned_by_player=True)]))
    from app.services.action_sequence_executor import ActionSequenceExecutor
    from app.services.context_compiler import ContextCompiler as ProductionCompiler
    from app.services.planning_context import action_reference_catalog
    await ActionSequenceExecutor(db_session).mark_applied(created.sequence_id)
    await db_session.commit()
    messages, _ = await ProductionCompiler(db_session).compile_context(cid, scene_id=scene)
    owned = action_reference_catalog(messages, "Player-owned items:")
    note_id = next(key for key, value in owned.items() if value == "Новая записка")
    wire = _intent_wire_model(messages)
    draft = wire.model_validate({"summary": "Отдать записку Алисе.", "actions": [{
        "evidence_quote": "Отдать записку Алисе.", "contribution_kind": "world_action", "actor_id": str(hero),
        "action_type": "inventory", "intent": "Отдать записку Алисе.", "item_id": note_id,
        "inventory_operation": "give", "inventory_target_id": str(npc)}]})
    from app.services.player_intent_interpreter import (
        PlayerIntentContractDraft,
        normalize_intent_draft,
    )
    normalized = normalize_intent_draft(PlayerIntentContractDraft.model_validate(draft.model_dump()), draft.summary)
    executed = await _execute(db_session, cid, scene, normalized,
        TurnOutcomeDecision(action_outcomes=[ActionOutcomeDecision(action_index=0, resolution="auto_success",
            observable_outcome="Алиса приняла записку.")]))
    assert executed.completed_steps == 1
    assert (await db_session.get(Item, note_id)).current_owner_id == str(npc)


def test_new_canon_schema_requires_a_registered_fact_subject_and_separate_position():
    from uuid import uuid4

    from app.services.canon_semantics import canon_envelope_for_entities
    subject = str(uuid4())
    wire = canon_envelope_for_entities({subject})
    proposal = {"outcome_id": "o", "change_type": "fact", "fact_type": "condition",
                "payload": {"subject": "Группа предметов", "object_value": "целые"}}
    with pytest.raises(ValidationError, match="subject_entity_id"):
        wire.model_validate({"proposals": [proposal]})
    proposal["subject_entity_id"] = subject
    assert wire.model_validate({"proposals": [proposal]}).proposals[0].subject_entity_id == subject
    proposal["fact_type"] = "holder"
    with pytest.raises(ValidationError, match="Physical position"):
        wire.model_validate({"proposals": [proposal]})


def test_npc_knowledge_window_does_not_erase_durable_beliefs():
    from uuid import uuid4
    start = datetime(2026, 1, 1, tzinfo=UTC)
    beliefs = [SimpleNamespace(id=uuid4(), updated_at=start + timedelta(minutes=i)) for i in range(300)]
    assert len(ContextCompiler._belief_window(beliefs)) == ContextCompiler.CHARACTER_BELIEF_WINDOW
    assert len(beliefs) == 300 and beliefs[-1] in ContextCompiler._belief_window(beliefs)


def test_known_npc_identity_is_selected_by_id_despite_an_inflected_designation():
    from uuid import uuid4

    from app.services.turn_outcome_resolver import _outcome_wire_model, normalize_outcome_draft
    person = str(uuid4())
    wire = _outcome_wire_model(0, known_cast={person: "Йонас Кель"})
    npc = {"canonical_name": "Келя", "cast_id": person, "role": "служащий",
           "description": "Знакомый служащий станции в аккуратном тёмном костюме.",
           "appearance": "Сухощавый мужчина в очках и тёмном костюме без украшений.",
           "reason": "Игрок обращается к известному служащему.", "temporary_name": False,
           "personal_name_evidence": "Йонас Кель"}
    draft = wire.model_validate({"action_outcomes": [], "npc_introductions": [npc],
                                 "observable_consequences": ["Игрок зовёт знакомого служащего."]})
    decision = normalize_outcome_draft(draft, PlayerIntentContract(summary="Позвать Келя."))
    assert str(decision.npc_introductions[0].existing_entity_id) == person
    assert decision.npc_introductions[0].canonical_name == "Йонас Кель"
    npc["cast_id"] = str(uuid4())
    with pytest.raises(ValidationError):
        wire.model_validate({"action_outcomes": [], "npc_introductions": [npc],
                             "observable_consequences": ["Игрок зовёт знакомого служащего."]})


@pytest.mark.asyncio
async def test_explicit_cast_binding_reuses_a_known_person_without_creating_an_alias_twin(db_session):
    from app.models.turn_authority import PlannedNpcIntroduction
    from app.services.turn_authority_resolvers import NpcIntroductionResolver

    cid, room, _, _, npc, _, _ = await _world(db_session)
    resolution = await NpcIntroductionResolver(db_session).resolve(
        campaign_id=cid, target_location_id=room, present_names=[],
        introductions=[PlannedNpcIntroduction(canonical_name="Госпожа Алиса", role="служащая",
            existing_entity_id=npc, personal_name_evidence="Алиса", reason="Известная собеседница отвечает.")])
    assert not resolution.new_introductions
    assert [arrival.entity_id for arrival in resolution.existing_arrivals] == [npc]


def test_inventory_validator_can_only_quote_actual_candidate_fragments():
    from app.services.turn_authority_validator import inventory_validation_model
    from tests.test_qa220_regressions import _auth

    authority = _auth()
    prose = "Бумага лежит на столе. Конверт остаётся закрытым."
    schema = inventory_validation_model(authority, prose).model_json_schema()
    claims = [value["properties"] for value in schema["$defs"].values()
              if "polarity" in value.get("properties", {})]
    assert len(claims) == len(authority.item_states)
    assert {claim["item_id"]["const"] for claim in claims} == {str(item.item_id) for item in authority.item_states}
    for claim in claims:
        evidence = claim["evidence"]
        assert set(evidence["enum"]) == {"Бумага лежит на столе.", "Конверт остаётся закрытым."}
        assert all(fragment in prose for fragment in evidence["enum"])


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "cancelled"])
async def test_terminal_generation_closes_its_unfinished_validation_audit(db_session, status):
    from app.db.narration_validation_table import NarrationValidationRun
    from app.db.repositories.job_repo import GenerationRunRepository
    from app.db.repositories.turn_repo import TurnRepository
    from app.models.turn import TurnCreate

    cid, _, _, _, _, _, scene = await _world(db_session)
    turn = await TurnRepository(db_session).create(cid, TurnCreate(role="user", content="Прочитать записку.", scene_id=scene))
    runs = GenerationRunRepository(db_session)
    run = await runs.create(cid, turn.id)
    audit = NarrationValidationRun(campaign_id=str(cid), trigger_turn_id=str(turn.id), scene_id=str(scene),
                                   status="validating", draft_text="Непроверенный текст.")
    db_session.add(audit)
    await db_session.flush()
    await runs.set_status(run.id, status, error="Validator failed")
    await db_session.refresh(audit)
    assert audit.status == status and audit.failure_reason == "Validator failed"
    assert audit.final_text is None
