from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.db.repositories.scene_repo import SceneRepository
from app.db.repositories.turn_repo import TurnRepository
from app.db.tables import Item
from app.models.item_state import ItemStateSnapshot
from app.models.narration_validation import NarrationValidationResult
from app.models.player_intent import (
    ActionOutcomeDecision,
    PlayerActionIntent,
    PlayerIntentContract,
    PlayerItemHandoff,
    TurnOutcomeDecision,
)
from app.models.proposed_change import ChangeType, ProposedChangeCreate
from app.models.turn import ChatMessage, TurnCreate
from app.models.turn_authority import TurnAuthority
from app.services.action_plan_compiler import ActionPlanCompiler
from app.services.action_sequence_executor import ActionSequenceExecutor
from app.services.authority_narration_pipeline import AuthorityNarrationPipeline
from app.services.continuity_checker import ContinuityChecker
from app.services.item_fact_guard import guard_item_facts
from app.services.item_state_authority import load_item_states
from app.services.narration_meta_guard import leaked_meta_lines
from app.services.player_intent_interpreter import _intent_semantic_review_wire, _intent_wire_model
from app.services.scene_lifecycle import SceneLifecycleService
from app.services.turn_authority_validator import TurnAuthorityValidator
from app.services.turn_planner import TurnPlanningError
from tests.test_qa216_item_receipts import setup_handoff

pytestmark = pytest.mark.interagent_contract_enforced


async def _world(session):
    cid, room, elsewhere, hero, npc, letter, scene = await setup_handoff(session)
    scenes = SceneRepository(session)
    await scenes.add_participant(scene, hero)
    await scenes.add_participant(scene, npc)
    await SceneLifecycleService(session).activate(cid, scene)
    return cid, room, elsewhere, hero, npc, letter, scene


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["located", "owned"])
async def test_static_item_claim_is_rejected_even_when_model_verdict_says_pass(mode):
    item, hero, npc, room = uuid4(), uuid4(), uuid4(), uuid4()
    text = "Письмо теперь находится у Марты за конторкой." if mode == "owned" else "Письмо лежит на конторке."
    router = SimpleNamespace(generate_json=AsyncMock(return_value={
        "verdict": "pass", "violations": [], "item_reviews": [{
            "item_id": str(item), "reference": "position", "evidence": text, "claims": [{
            "item_id": str(item), "evidence": text, "scope": "current", "mode": mode,
            "owner_id": str(npc) if mode == "owned" else None,
            "location_id": str(room) if mode == "located" else None,
        }]}],
    }))
    auth = TurnAuthority(
        campaign_id=uuid4(), trigger_turn_id=uuid4(), player_input="Где письмо?",
        item_states=[ItemStateSnapshot(item_id=item, name="Письмо", owner_id=hero, owner_name="Ирма")],
        observable_consequences=["Марта согласилась помочь."],
    )
    result = await TurnAuthorityValidator(router).validate(SimpleNamespace(), auth, text)
    assert result.verdict == "repair_required"
    assert result.violations[0].violation_type == "canon_conflict"


@pytest.mark.asyncio
async def test_quoted_past_position_does_not_override_current_owner():
    item, hero, room = uuid4(), uuid4(), uuid4()
    text = "— Вчера письмо лежало на конторке, — вспоминает Марта."
    router = SimpleNamespace(generate_json=AsyncMock(return_value={
        "verdict": "pass", "violations": [], "item_reviews": [{
            "item_id": str(item), "reference": "position", "evidence": text, "claims": [{
            "item_id": str(item), "evidence": "Вчера письмо лежало на конторке", "scope": "past",
            "mode": "located", "location_id": str(room),
        }]}],
    }))
    auth = TurnAuthority(campaign_id=uuid4(), trigger_turn_id=uuid4(), player_input="Где письмо?",
                         item_states=[ItemStateSnapshot(item_id=item, name="Письмо", owner_id=hero)])
    assert (await TurnAuthorityValidator(router).validate(SimpleNamespace(), auth, text)).verdict == "pass"


@pytest.mark.asyncio
async def test_real_publication_path_cannot_bypass_inventory_guard(db_session, monkeypatch):
    cid, _, _, hero, _, letter, scene = await _world(db_session)
    user = await TurnRepository(db_session).create(cid, TurnCreate(role="user", content="Возьмите письмо", scene_id=scene))
    row = await db_session.get(Item, str(letter))
    row.current_owner_id = str(hero)
    await db_session.flush()
    auth = TurnAuthority(
        campaign_id=cid, trigger_turn_id=user.id, player_input=user.content,
        object_names=["письмо Оста Эндлина"], item_states=await load_item_states(db_session, cid),
        action_sequence={"steps": [{"action_type": "service", "status": "completed",
                                    "observable_outcome": "Алиса передаёт письмо Оста Эндлина."}]},
    )
    pipeline = AuthorityNarrationPipeline(db_session, SimpleNamespace(
        resolve=AsyncMock(return_value=SimpleNamespace(config=SimpleNamespace(model_name="test"))),
    ))
    monkeypatch.setattr(pipeline, "_narrate", AsyncMock(return_value=(
        "Алиса передаёт письмо Оста Эндлина.", None, {},
    )))
    monkeypatch.setattr(TurnAuthorityValidator, "validate", AsyncMock(return_value=NarrationValidationResult(verdict="pass")))
    result = await pipeline.generate(
        campaign_id=cid, trigger_turn_id=user.id, scene_id=scene, narrator_messages=[],
        narrator_selection=SimpleNamespace(config=SimpleNamespace()), authority=auth,
    )
    assert "передаёт" not in result.text
    assert "Вера" in result.text
    assert result.telemetry["narration_validation"]["publication_guard"]["mode"] == "authority_projection"


@pytest.mark.asyncio
async def test_repeated_false_static_claim_falls_back_to_actual_item_state(db_session, monkeypatch):
    cid, room, _, hero, _, letter, scene = await _world(db_session)
    item = await db_session.get(Item, str(letter))
    item.current_owner_id = str(hero)
    await db_session.flush()
    user = await TurnRepository(db_session).create(cid, TurnCreate(role="user", content="Где письмо?", scene_id=scene))
    false_text = "Письмо Оста Эндлина лежит на конторке."
    router = SimpleNamespace(
        resolve=AsyncMock(return_value=SimpleNamespace(config=SimpleNamespace(model_name="test"))),
        generate_json=AsyncMock(return_value={
            "verdict": "pass", "violations": [], "item_reviews": [{
                "item_id": str(letter), "reference": "position", "evidence": false_text, "claims": [{
                "item_id": str(letter), "evidence": false_text, "scope": "current",
                "mode": "located", "location_id": str(room),
            }]}],
        }),
    )
    auth = TurnAuthority(
        campaign_id=cid, trigger_turn_id=user.id, player_input=user.content,
        item_states=await load_item_states(db_session, cid), observable_consequences=[false_text],
    )
    pipeline = AuthorityNarrationPipeline(db_session, router)
    monkeypatch.setattr(pipeline, "_narrate", AsyncMock(return_value=(false_text, None, {})))
    result = await pipeline.generate(
        campaign_id=cid, trigger_turn_id=user.id, scene_id=scene, narrator_messages=[],
        narrator_selection=SimpleNamespace(config=SimpleNamespace()), authority=auth,
    )
    assert false_text not in result.text
    assert "Вера" in result.text
    assert result.validation_status == "safe_fallback"
    assert result.telemetry["narration_validation"]["publication_guard"]["mode"] == "item_state_projection"
    assert router.generate_json.await_count == 2


@pytest.mark.asyncio
async def test_validator_outage_never_fail_opens_a_static_item_claim(db_session, monkeypatch):
    cid, _, _, hero, _, letter, scene = await _world(db_session)
    row = await db_session.get(Item, str(letter))
    row.current_owner_id = str(hero)
    await db_session.flush()
    user = await TurnRepository(db_session).create(cid, TurnCreate(role="user", content="Где письмо?", scene_id=scene))
    auth = TurnAuthority(campaign_id=cid, trigger_turn_id=user.id, player_input=user.content,
                         item_states=await load_item_states(db_session, cid))
    pipeline = AuthorityNarrationPipeline(db_session, SimpleNamespace(resolve=AsyncMock(return_value=None)))
    monkeypatch.setattr(pipeline, "_narrate", AsyncMock(return_value=("Письмо лежит на конторке.", None, {})))
    result = await pipeline.generate(
        campaign_id=cid, trigger_turn_id=user.id, scene_id=scene, narrator_messages=[],
        narrator_selection=SimpleNamespace(config=SimpleNamespace()), authority=auth,
    )
    assert "на конторке" not in result.text
    assert "Вера" in result.text
    assert result.telemetry["narration_validation"]["publication_guard"]["mode"] == "item_state_projection"


@pytest.mark.asyncio
async def test_confirmed_player_offer_gets_give_receipt_and_changes_owner(db_session):
    cid, _, _, hero, npc, letter, scene = await _world(db_session)
    item = await db_session.get(Item, str(letter))
    item.current_owner_id = str(hero)
    await db_session.flush()
    offer = "Алиса, возьмите письмо на хранение."
    accepted = "Алиса принимает письмо на хранение."
    contract = PlayerIntentContract(summary=offer, actions=[PlayerActionIntent(action_type="service", intent=offer)])
    decision = TurnOutcomeDecision(
        action_outcomes=[ActionOutcomeDecision(action_index=0, resolution="auto_success", observable_outcome=accepted)],
        player_item_handoffs=[PlayerItemHandoff(item_id=letter, recipient_id=npc,
            offer_evidence=offer, evidence=accepted, after_action_index=0)],
    )
    plan = await ActionPlanCompiler(db_session).compile(cid, contract, decision, player_input=offer)
    user = await TurnRepository(db_session).create(cid, TurnCreate(role="user", content=offer, scene_id=scene))
    report = await ActionSequenceExecutor(db_session).execute(cid, scene, user.id, plan.action_sequence)
    assert item.current_owner_id == str(npc)
    receipt = report.steps[-1]
    assert receipt.action_type == "inventory"
    assert receipt.item_operation == "give"
    assert receipt.item_previous_owner_id == hero
    assert receipt.item_result_owner_id == npc


@pytest.mark.asyncio
async def test_npc_consent_without_a_player_offer_cannot_create_give(db_session):
    cid, _, _, _, npc, letter, _ = await _world(db_session)
    text = "Вы могли бы хранить письмо?"
    contract = PlayerIntentContract(summary=text, actions=[])
    decision = TurnOutcomeDecision(observable_consequences=["Алиса принимает письмо."], player_item_handoffs=[
        PlayerItemHandoff(item_id=letter, recipient_id=npc, offer_evidence="Отдаю письмо Алисе.", evidence="Алиса принимает письмо."),
    ])
    with pytest.raises(TurnPlanningError, match="committed player offer"):
        await ActionPlanCompiler(db_session).compile(cid, contract, decision, player_input=text)


@pytest.mark.asyncio
async def test_scribe_cannot_store_parallel_placement_fact_but_keeps_item_description(db_session):
    cid, _, _, _, _, _, _ = await _world(db_session)
    proposals = [ProposedChangeCreate(change_type=ChangeType.FACT, payload={
        "subject": "письмо Оста Эндлина", "predicate": predicate, "object_value": value,
    }) for predicate, value in [("положение", "на конторке"), ("цвет печати", "красный")]]
    router = SimpleNamespace(resolve=AsyncMock(return_value=SimpleNamespace()), generate_json=AsyncMock(return_value={
        "decisions": [{"proposal_index": 0, "domain": "custody_or_placement"},
                      {"proposal_index": 1, "domain": "other"}],
    }))
    guarded = await guard_item_facts(db_session, cid, proposals, router=router)
    assert not (await ContinuityChecker(db_session).validate_change(cid, guarded[0]))[0]
    assert (await ContinuityChecker(db_session).validate_change(cid, guarded[1]))[0]


def test_outside_item_remains_selectable_in_both_intent_schemas():
    hero, npc, outside, held = [str(uuid4()) for _ in range(4)]
    context = [ChatMessage(role="system", content=(
        f"Controlled character: {hero}\nPhysically present characters: Ирма [id={hero}], Марта [id={npc}]\n"
        f"Player-owned items: письмо в рамке [id={held}]\nRegistered items: письмо без марки [id={outside}], письмо в рамке [id={held}]"
    ))]
    action = {"action_type": "inventory", "actor_role": "speaker", "intent": "Забрать письмо без марки",
              "item_id": outside, "inventory_operation": "take"}
    assert _intent_wire_model(context).model_validate({"summary": action["intent"], "actions": [action]}).actions[0].item_id == outside
    review = _intent_semantic_review_wire(1, action["intent"], "Ирма", context)
    assert review.model_validate({"action_ownership": [{**{key: value for key, value in action.items() if key != "intent"}, "action_index": 0,
        "spatial_effect": "local", "contribution_kind": "world_action"}],
        "information_request_only": False, "information_recipient": "none"}).action_ownership[0].item_id is not None


@pytest.mark.asyncio
async def test_outside_take_keeps_attempted_id_and_reports_actual_absence(db_session):
    from app.services.turn_planner import ActionSequencePlan, ActionStepPlan
    cid, _, elsewhere, _, _, letter, scene = await _world(db_session)
    item = await db_session.get(Item, str(letter))
    item.current_owner_id = None
    item.current_location_id = str(elsewhere)
    await db_session.flush()
    user = await TurnRepository(db_session).create(cid, TurnCreate(role="user", content="Беру письмо", scene_id=scene))
    report = await ActionSequenceExecutor(db_session).execute(cid, scene, user.id, ActionSequencePlan(steps=[
        ActionStepPlan(action_type="inventory", intent=user.content, resolution="auto_success",
                       item_id=letter, inventory_operation="take", observable_outcome="Письмо взято."),
    ]))
    assert report.steps[0].status == "blocked"
    assert report.steps[0].item_id == letter
    assert "в другом месте" in report.steps[0].blocking_reason
    assert item.current_location_id == str(elsewhere)


@pytest.mark.asyncio
async def test_campaign_provider_check_has_structured_auth_failure(client, monkeypatch):
    from app.services.campaign_service import CampaignService
    from app.services.chatgpt_auth_service import ChatGPTAuthError
    monkeypatch.setattr(CampaignService, "check_provider_connection", AsyncMock(side_effect=ChatGPTAuthError("Переподключите аккаунт")))
    response = client.post(f"/api/campaigns/{uuid4()}/provider/check")
    assert response.status_code == 200
    assert response.json() == {"connected": False, "code": "provider_auth_required",
                               "message": "Переподключите аккаунт", "retryable": False}


def test_crash_diagnostics_default_is_in_data_dir(tmp_path):
    env = dict(os.environ)
    env.pop("PDM_CRASH_LOG", None)
    env["PDM_DATA_DIR"] = str(tmp_path / "data")
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    script = (
        "import logging; from app.runtime import _install_crash_diagnostics; "
        "_install_crash_diagnostics(); logging.getLogger('uvicorn.error').error('QA219 log probe'); "
        "logging.shutdown()"
    )
    subprocess.run([sys.executable, "-c", script], cwd=tmp_path, env=env, capture_output=True, check=True)
    assert "QA219 log probe" in (tmp_path / "data" / "personal-dm-crash-errors.log").read_text(encoding="utf-8")


def test_engine_state_wording_is_not_publishable_prose():
    assert leaked_meta_lines("В авторитетном состоянии сцены письмо числится у Ирмы.")


async def _heard_introduction(session):
    from app.services.canon_applier import CanonApplier
    from app.services.player_introduction_memory import introduction_knowledge
    cid, _, _, hero, npc, _, scene = await _world(session)
    turns = TurnRepository(session)
    evidence = "Я Вера, пришла за своей почтой."
    user = await turns.create(cid, TurnCreate(role="user", content=evidence, scene_id=scene))
    snapshot = {"turn_planner": {"telemetry": {"intent_contract": {
        "spoken_identity_evidence": evidence,
    }}}, "turn_authority": {"beat_owner_id": str(npc)}}
    assistant = await turns.create(cid, TurnCreate(
        role="assistant", content="Алиса кивает и приветствует Веру.", scene_id=scene,
        parent_turn_id=user.id, context_snapshot=snapshot,
    ))
    proposals = await introduction_knowledge(session, cid, assistant, user, snapshot)
    assert len(proposals) == 1
    assert proposals[0].payload["source_character_id"] == str(hero)
    await CanonApplier(session).apply(cid, ChangeType.KNOWLEDGE, proposals[0].payload, assistant.id)
    return cid, npc, scene, evidence


@pytest.mark.asyncio
async def test_present_npc_keeps_heard_introduction_after_short_history_expires(db_session):
    from app.services.base_context_compiler import ContextCompiler
    cid, _, scene, evidence = await _heard_introduction(db_session)
    turns = TurnRepository(db_session)
    for _ in range(16):
        await turns.create(cid, TurnCreate(role="user", content="Осматриваю окно.", scene_id=scene))
    messages, _ = await ContextCompiler(db_session).compile_context(cid, scene_id=scene)
    assert evidence in messages[0].content
    assert all(evidence not in message.content for message in messages[1:])


@pytest.mark.asyncio
async def test_undo_removes_knowledge_of_an_undone_introduction(db_session):
    from app.db.repositories.belief_repo import BeliefRepository
    from app.services.turn_undo_service import TurnUndoService
    cid, npc, _, evidence = await _heard_introduction(db_session)
    assert evidence in (await BeliefRepository(db_session).get_for_character(npc))[0].proposition
    assert await TurnUndoService(db_session).undo_last_pair(cid)
    assert await BeliefRepository(db_session).get_for_character(npc) == []
