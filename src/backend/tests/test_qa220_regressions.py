from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.db.repositories.turn_repo import TurnRepository
from app.db.tables import Item
from app.models.item_state import ItemPositionClaim, ItemStateSnapshot
from app.models.narration_validation import NarrationValidationResult
from app.models.player_intent import PlayerActionIntent, PlayerIntentContract
from app.models.turn import TurnCreate
from app.models.turn_authority import TurnAuthority
from app.services.action_plan_compiler import ActionPlanCompiler
from app.services.action_sequence_executor import ActionSequenceExecutor
from app.services.authority_narration_pipeline import AuthorityNarrationPipeline
from app.services.item_state_authority import item_position_violations, load_item_states
from app.services.narration_publication_guard import NarrationPublicationGuard
from app.services.narration_validator import NarrationValidationError
from app.services.turn_authority_planner import CoordinatedTurnPlan
from app.services.turn_authority_service import TurnAuthorityService
from app.services.turn_authority_validator import TurnAuthorityValidator
from app.services.turn_outcome_resolver import _outcome_wire_model, normalize_outcome_draft
from app.services.turn_planner import ActionSequencePlan, ActionStepPlan
from tests.test_interagent_turn_authority import _world as _empty_world
from tests.test_qa219_regressions import _world

pytestmark = pytest.mark.interagent_contract_enforced


def _auth():
    item, owner, room = uuid4(), uuid4(), uuid4()
    return TurnAuthority(
        campaign_id=uuid4(), trigger_turn_id=uuid4(), player_input="Осматриваю ключ, не трогая его.",
        object_names=["Ключ от чердака"], item_states=[ItemStateSnapshot(
            item_id=item, name="Ключ от чердака", owner_id=owner, owner_name="Осип Ким",
            placement_detail="Висит на ключевой доске под контролем Осипа.",
            owner_location_id=room,
        )],
    )


@pytest.mark.asyncio
async def test_owned_key_can_still_hang_on_its_established_board():
    auth = _auth()
    state = auth.item_states[0]
    text = "Ключ от чердака по-прежнему висит на доске. Осип придерживает связку рукой."
    router = SimpleNamespace(generate_json=AsyncMock(return_value={
        "verdict": "pass", "position_claims": [{
            "item_id": str(state.item_id), "evidence": "Ключ от чердака по-прежнему висит на доске",
            "change": "moved", "scope": "current", "aspect": "placement", "mode": "located",
            "location_id": str(state.owner_location_id), "placement_matches": True,
        }],
    }))
    result = await TurnAuthorityValidator(router).validate(SimpleNamespace(), auth, text)
    assert result.verdict == "pass"
    assert NarrationPublicationGuard.publish(auth, text, result)[0] == text


@pytest.mark.asyncio
async def test_custody_change_cannot_hide_behind_the_same_board():
    auth = _auth()
    state = auth.item_states[0]
    text = "Ключ от чердака теперь у Ирмы."
    router = SimpleNamespace(generate_json=AsyncMock(return_value={
        "verdict": "pass", "position_claims": [{
            "item_id": str(state.item_id), "evidence": text, "change": "moved", "scope": "current",
            "aspect": "custody", "mode": "owned", "owner_id": str(uuid4()),
            "placement_matches": True,
        }],
    }))
    result = await TurnAuthorityValidator(router).validate(SimpleNamespace(), auth, text)
    assert result.verdict == "repair_required"


@pytest.mark.asyncio
@pytest.mark.parametrize("completed", [True, False])
async def test_take_animation_uses_its_real_before_state_only_when_completed(completed):
    item, hero, room = uuid4(), uuid4(), uuid4()
    text = "Ты снимаешь письмо с металла и берёшь в руку."
    auth = TurnAuthority(campaign_id=uuid4(), trigger_turn_id=uuid4(), player_input="Забираю письмо.",
        item_states=[ItemStateSnapshot(item_id=item, name="Письмо", owner_id=hero)],
        action_sequence={"steps": [{"step_index": 0, "action_type": "inventory",
            "status": "completed" if completed else "blocked", "item_id": str(item),
            "item_operation": "take", "item_previous_location_id": str(room),
            "item_previous_placement_detail": "На металлических перилах",
            "item_result_owner_id": str(hero), "observable_outcome": "Письмо взято."}]})
    router = SimpleNamespace(generate_json=AsyncMock(return_value={
        "verdict": "pass", "position_claims": [{
            "item_id": str(item), "evidence": "Ты снимаешь письмо с металла",
            "change": "moved", "scope": "executed_action", "action_step_index": 0, "moment": "before",
            "mode": "located", "location_id": str(room),
        }],
    }))
    result = await TurnAuthorityValidator(router).validate(SimpleNamespace(), auth, text)
    assert result.verdict == ("pass" if completed else "repair_required")
    if completed:
        assert NarrationPublicationGuard.publish(auth, text, result)[0] == text


@pytest.mark.asyncio
async def test_truthful_refusal_preserves_question_and_dialogue_after_repair(db_session, monkeypatch):
    cid, _, _, _, _, _, scene = await _world(db_session)
    user = await TurnRepository(db_session).create(cid, TurnCreate(
        role="user", content="Возьмите письмо.", scene_id=scene,
    ))
    auth = TurnAuthority(campaign_id=cid, trigger_turn_id=user.id, player_input=user.content,
        item_states=await load_item_states(db_session, cid), object_names=["письмо Оста Эндлина"],
        action_sequence={"steps": [{"action_type": "inventory", "status": "blocked",
            "public_blocking_reason": "Нужно уточнить предмет."}]})
    prose = "Алиса протягивает ладонь, но останавливается. — Какое письмо вы хотите оставить?"
    router = SimpleNamespace(
        resolve=AsyncMock(return_value=SimpleNamespace(config=SimpleNamespace(model_name="test"))),
        generate_json=AsyncMock(side_effect=[
            {"verdict": "repair_required", "violations": [{
                "violation_type": "player_agency", "evidence": "Ты отдаёшь письмо.",
                "correction": "Оставь выбор игроку.",
            }], "position_claims": []},
            {"verdict": "pass", "outcome_coverage": {"result_0": "P0"},
             "position_claims": []},
        ]),
    )
    pipeline = AuthorityNarrationPipeline(db_session, router)
    monkeypatch.setattr(pipeline, "_narrate", AsyncMock(side_effect=[
        ("Ты отдаёшь письмо.", None, {}), (prose, None, {}),
    ]))
    result = await pipeline.generate(campaign_id=cid, trigger_turn_id=user.id, scene_id=scene,
        narrator_messages=[], narrator_selection=SimpleNamespace(config=SimpleNamespace()), authority=auth)
    assert result.text == prose
    assert result.validation_status == "repaired"
    assert result.telemetry["narration_validation"]["publication_guard"]["mode"] == "validated_candidate"


def test_model_cannot_forge_a_completed_item_check_in_json():
    verdict = NarrationValidationResult.model_validate({"verdict": "pass", "_item_states_verified": True})
    assert verdict._item_states_verified is False


@pytest.mark.asyncio
async def test_loaded_empty_inventory_preserves_valid_ambient_narration(db_session):
    cid, hero, _, scene = await _empty_world(db_session)
    auth = await TurnAuthorityService(db_session).build(
        campaign_id=cid, trigger_turn_id=uuid4(), player_input="Стучу в дверь и прислушиваюсь.",
        source_scene_id=scene.id, target_scene_id=scene.id, acting_character_id=hero.id,
        plan=CoordinatedTurnPlan(player_intent="Прислушаться.", resolution="conversation",
                                 observable_consequences=["За дверью слышен скрип."]),
    )
    assert auth.item_states == [] and auth.item_states_loaded
    auth.object_names = ["Дверь фабрики"]
    text = "Эхо передаёт твой стук по пустому коридору. За дверью скрипит дерево."
    router = SimpleNamespace(generate_json=AsyncMock(return_value={"verdict": "pass"}))
    result = await TurnAuthorityValidator(router).validate(SimpleNamespace(), auth, text)
    assert NarrationPublicationGuard.publish(auth, text, result)[0] == text
    assert router.generate_json.await_count == 1


@pytest.mark.asyncio
async def test_complete_empty_inventory_does_not_override_a_real_canon_error():
    auth = TurnAuthority(campaign_id=uuid4(), trigger_turn_id=uuid4(), player_input="Слушаю.",
                         item_states_loaded=True, observable_consequences=["Дверь закрыта."])
    text = "Ты оказываешься в другом городе."
    router = SimpleNamespace(generate_json=AsyncMock(return_value={"verdict": "repair_required",
        "violations": [{"violation_type": "invalid_movement", "evidence": text,
                        "correction": "Оставь героя на месте."}]}))
    result = await TurnAuthorityValidator(router).validate(SimpleNamespace(), auth, text)
    assert result.verdict == "repair_required"
    assert NarrationPublicationGuard.publish(auth, text, result)[0] != text


@pytest.mark.asyncio
async def test_an_invented_claim_excerpt_cannot_authorize_publication():
    auth = _auth()
    state = auth.item_states[0]
    text = "Ключ от чердака лежит на конторке."
    router = SimpleNamespace(generate_json=AsyncMock(return_value={"position_claims": [{
        "item_id": str(state.item_id), "evidence": "Придуманная цитата", "change": "moved", "scope": "current", "mode": "located"}]}))
    with pytest.raises(NarrationValidationError):
        await TurnAuthorityValidator(router).validate(SimpleNamespace(), auth, text)


@pytest.mark.asyncio
async def test_exact_excerpts_wrapped_in_quotes_do_not_discard_valid_prose():
    auth = _auth()
    text = "Ключ от чердака по-прежнему висит на доске."
    state = auth.item_states[0]
    router = SimpleNamespace(generate_json=AsyncMock(return_value={
        "verdict": "pass", "position_claims": [{"item_id": str(state.item_id), "evidence": f"«{text}»",
                "change": "moved", "scope": "current", "aspect": "placement", "mode": "located", "placement_matches": True}],
    }))
    result = await TurnAuthorityValidator(router).validate(SimpleNamespace(), auth, text)
    assert result.verdict == "pass"
    assert NarrationPublicationGuard.publish(auth, text, result)[0] == text


def test_completed_intermediate_holding_is_valid_only_inside_its_receipt():
    item, hero, npc = uuid4(), uuid4(), uuid4()
    state = ItemStateSnapshot(item_id=item, name="Письмо", owner_id=npc)
    text = "Ты берёшь письмо в руку, затем передаёшь его Марте."
    claim = ItemPositionClaim(item_id=item, evidence="берёшь письмо в руку", scope="executed_action",
                              action_step_index=0, moment="after", mode="owned", owner_id=hero)
    receipts = [{"step_index": 0, "item_id": str(item), "item_result_owner_id": str(hero)},
                {"step_index": 1, "item_id": str(item), "item_result_owner_id": str(npc)}]
    assert not item_position_violations([state], [claim], text, receipts)
    claim.scope = "current"
    assert item_position_violations([state], [claim], text, receipts)


@pytest.mark.asyncio
async def test_ownerless_counter_claim_after_take_is_still_rejected():
    auth = _auth()
    state = auth.item_states[0]
    state.placement_detail = None
    text = "Ключ лежит на столе перед героиней."
    router = SimpleNamespace(generate_json=AsyncMock(return_value={
        "verdict": "pass", "position_claims": [{
            "item_id": str(state.item_id), "evidence": text, "change": "moved", "scope": "current",
            "aspect": "placement", "mode": "located", "location_id": str(state.owner_location_id),
        }],
    }))
    assert (await TurnAuthorityValidator(router).validate(SimpleNamespace(), auth, text)).verdict == "repair_required"


@pytest.mark.asyncio
@pytest.mark.parametrize("intent", ["Алиса, возьмите письмо на хранение.", "Передаю Алисе письмо на хранение."])
async def test_player_owned_letter_cannot_decode_as_reverse_receive(db_session, intent):
    cid, _, _, hero, npc, item, scene = await _world(db_session)
    row = await db_session.get(Item, str(item))
    row.current_owner_id = str(hero)
    await db_session.flush()
    wire = _outcome_wire_model(1, handoff_states=await load_item_states(db_session, cid),
                               controlled_id=str(hero), present_ids={str(hero), str(npc)})
    common = {"action_outcomes": [{"action_index": 0, "resolution": "auto_success",
                                   "observable_outcome": "Алиса принимает письмо на хранение."}],
              "npc_introductions": []}
    with pytest.raises(ValidationError):
        wire.model_validate({**common, "npc_item_handoffs": [{"item_id": str(item),
            "giver_id": str(npc), "evidence": "Алиса принимает письмо на хранение."}]})
    draft = wire.model_validate({**common, "npc_item_handoffs": [], "player_item_handoffs": [{
        "item_id": str(item), "recipient_id": str(npc), "offer_evidence": intent,
        "evidence": "Алиса принимает письмо на хранение.", "after_action_index": 0,
    }]})
    contract = PlayerIntentContract(summary=intent, actions=[PlayerActionIntent(action_type="service", intent=intent)])
    plan = await ActionPlanCompiler(db_session).compile(cid, contract, normalize_outcome_draft(draft, contract), player_input=intent)
    user = await TurnRepository(db_session).create(cid, TurnCreate(role="user", content=intent, scene_id=scene))
    report = await ActionSequenceExecutor(db_session).execute(cid, scene, user.id, plan.action_sequence)
    assert report.steps[-1].item_operation == "give"
    assert row.current_owner_id == str(npc)


@pytest.mark.asyncio
async def test_resolver_block_keeps_item_attempt_and_placement_without_mutation(db_session):
    cid, _, elsewhere, _, _, letter, scene = await _world(db_session)
    item = await db_session.get(Item, str(letter))
    item.current_owner_id = None
    item.current_location_id = str(elsewhere)
    item.placement_detail = "На перилах"
    await db_session.flush()
    user = await TurnRepository(db_session).create(cid, TurnCreate(role="user", content="Беру письмо.", scene_id=scene))
    report = await ActionSequenceExecutor(db_session).execute(cid, scene, user.id, ActionSequencePlan(steps=[
        ActionStepPlan(action_type="inventory", intent=user.content, item_id=letter,
                       inventory_operation="take", resolution="blocked", blocking_reason="Письмо снаружи."),
    ]))
    step = report.steps[0]
    assert step.status == "blocked" and step.item_id == letter and step.item_operation == "take"
    assert step.item_previous_location_id == elsewhere
    assert step.item_previous_placement_detail == "На перилах"
    assert step.item_result_owner_id is None
    assert item.current_location_id == str(elsewhere)
