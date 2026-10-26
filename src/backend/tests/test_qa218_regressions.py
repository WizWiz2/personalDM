"""Regression cases reconstructed from QA-218's recorded player turns."""
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.db.repositories.scene_repo import SceneRepository
from app.db.tables import Item
from app.models.narration_validation import NarrationValidationResult
from app.models.session_zero import SessionZeroUpdate
from app.models.turn_authority import TurnAuthority
from app.services.action_sequence_executor import ActionSequenceExecutor
from app.services.narration_publication_guard import NarrationPublicationGuard
from app.services.playable_bootstrap import PlayableBootstrapService
from app.services.player_intent_interpreter import (
    PlayerActionIntentDraft,
    PlayerIntentContractDraft,
    normalize_intent_draft,
)
from app.services.scene_lifecycle import SceneLifecycleService
from app.services.session_zero_service import SessionZeroService
from app.services.turn_planner import ActionStepPlan
from tests.test_qa216_item_receipts import setup_handoff


@pytest.mark.asyncio
@pytest.mark.session_zero_enforced
async def test_bootstrap_npc_mail_can_be_handed_over_without_manual_canon_patch(db_session):
    cid, room, _, hero, npc, _, scene = await setup_handoff(db_session)
    await SceneRepository(db_session).add_participant(scene, hero)
    await SceneRepository(db_session).add_participant(scene, npc)
    await SessionZeroService(db_session).update(cid, SessionZeroUpdate(custom_fields={
        "starter_items": [{"canonical_name": "Письмо в траурной рамке",
                           "held_by_npc": "Алиса", "initial_placement": "На конторке Алисы"}],
    }))
    await SceneLifecycleService(db_session).activate(cid, scene)
    bootstrap = PlayableBootstrapService(db_session)
    result = await bootstrap.ensure(
        cid, scene, player_character_id=hero, starting_location_id=room,
        starting_situation="Алиса хранит почту и добровольно отдаёт её адресату.",
    )
    item = await db_session.get(Item, str(result.created_object_id))
    assert item.current_owner_id == str(npc)
    assert item.current_location_id is None
    recorded = SimpleNamespace()
    await ActionSequenceExecutor(db_session)._apply_inventory_step(
        cid, scene, uuid4(), ActionStepPlan(
            action_type="inventory", intent="Получить свою почту у Алисы",
            resolution="auto_success", observable_outcome="Алиса передаёт письмо.",
            item_id=result.created_object_id, inventory_operation="receive", inventory_target_id=npc,
        ), recorded,
    )
    assert item.current_owner_id == str(hero)
    assert recorded.item_previous_owner_id == str(npc)
    assert recorded.item_result_owner_id == str(hero)
    await bootstrap.ensure(
        cid, scene, player_character_id=hero, starting_location_id=room,
        starting_situation="Алиса хранит почту и добровольно отдаёт её адресату.",
    )
    assert item.current_owner_id == str(hero)  # Retry cannot steal it back.


def _draft(text):
    return PlayerIntentContractDraft(summary=text, actions=[
        PlayerActionIntentDraft(action_type="interaction", intent=text),
    ])


def test_explicit_take_selects_distinguished_letter_and_freezes_inventory_operation():
    letter = uuid4()
    text = "Марта, спасибо. Раз письмо у края — забираю письмо в траурной рамке с конторки в руку."
    draft = PlayerIntentContractDraft(summary=text, actions=[PlayerActionIntentDraft(
        action_type="inventory", intent=text, item_id=str(letter), inventory_operation="take")])
    action = normalize_intent_draft(draft, text).actions[0]
    assert action.action_type == "inventory"
    assert action.inventory_operation == "take"
    assert action.item_id == letter


@pytest.mark.parametrize("text", ["Не забираю письмо.", "Забираю письмо?", "Если забираю письмо, что будет?",
                                  "Забираю письмо."])
def test_take_repair_does_not_invent_authority_or_choose_between_similar_letters(text):
    result = normalize_intent_draft(_draft(text), text)
    assert result.actions[0].item_id is None
    assert result.actions[0].inventory_operation is None


def test_vague_upward_gesture_does_not_fail_or_authorize_a_new_placement():
    text = "Протягиваю письмо без марки вверх, в сторону четвёртого номера."
    draft = PlayerIntentContractDraft(summary=text, actions=[PlayerActionIntentDraft(
        action_type="inventory", intent=text, item_id=str(uuid4()),
        inventory_operation="reposition",
    )])
    action = normalize_intent_draft(draft, text).actions[0]
    assert action.action_type == "observation"
    assert action.item_id is None
    assert action.item_placement_detail is None


@pytest.mark.parametrize("candidate", [
    "Марта передаёт письмо в траурной рамке Ирме.",
    "Письмо в траурной рамке оказывается у вас в руке.",
    "Письмо в траурной рамке переходит к тебе.",
])
def test_third_person_and_impersonal_handoffs_require_executed_inventory(candidate):
    auth = TurnAuthority(
        campaign_id=uuid4(), trigger_turn_id=uuid4(), player_input="Забираю письмо.",
        object_names=["письмо в траурной рамке"],
        observable_consequences=["Письмо остаётся на конторке."],
    )
    published, meta = NarrationPublicationGuard.publish(
        auth, candidate, NarrationValidationResult(verdict="pass"),
    )
    assert meta["unsupported_item_claim"]
    assert published == "Письмо остаётся на конторке."


def test_wrong_encryption_key_has_reconnect_guidance(monkeypatch, tmp_path):
    from app.services.chatgpt_auth_service import ChatGPTAuthError, ChatGPTAuthService
    monkeypatch.setattr("app.services.chatgpt_auth_service.settings.DATA_DIR", str(tmp_path))
    service = ChatGPTAuthService()
    service.profile_file.parent.mkdir(parents=True, exist_ok=True)
    service.profile_file.write_text("encrypted", encoding="utf-8")
    def fail(_value):
        raise RuntimeError("Stored provider secret cannot be decrypted with the current key")
    monkeypatch.setattr("app.services.chatgpt_auth_service.decrypt_secret", fail)
    with pytest.raises(ChatGPTAuthError, match="переподключите аккаунт"):
        service._load_profile()


@pytest.mark.asyncio
async def test_existing_exit_overrides_new_destination_binding():
    from unittest.mock import AsyncMock

    from app.services.player_intent_interpreter import PlayerIntentInterpreter
    place = str(uuid4())
    router = SimpleNamespace(generate_json=AsyncMock(return_value={"action_0": "new"}))
    interpreter = PlayerIntentInterpreter(router)
    draft = PlayerIntentContractDraft(summary="Выхожу наружу.", actions=[PlayerActionIntentDraft(
        action_type="movement", intent="Выйти наружу", destination_location="наружу",
    )])
    await interpreter._bind_destinations(
        SimpleNamespace(), draft, draft.summary, {place: "Окрестности — гостиница"},
        catalog={place: {"exit_labels_from_current": ["наружу"]}},
    )
    action = normalize_intent_draft(draft, draft.summary).actions[0]
    assert action.destination_location_id == UUID(place)
    assert action.destination_location == "Окрестности — гостиница"


@pytest.mark.asyncio
async def test_cancelled_transfer_is_hidden_from_active_feed_but_keeps_audit_status(db_session):
    from app.db.repositories.event_repo import EventRepository
    from app.db.repositories.turn_repo import TurnRepository
    from app.models.truth_engine import CanonicalEventCreate
    from app.models.turn import TurnCreate
    from app.services.truth_engine import CanonicalEventStore
    cid, _, _, _, _, _, scene = await setup_handoff(db_session)
    turn = await TurnRepository(db_session).create(cid, TurnCreate(
        role="user", content="Передаю письмо", scene_id=scene,
    ))
    store = CanonicalEventStore(db_session)
    event = await store.append(cid, CanonicalEventCreate(
        event_key="receipt:transfer", event_type="item_transfer", description="Письмо передано.",
        source_kind="executor_receipt", source_turn_id=turn.id,
    ))
    repo = EventRepository(db_session)
    assert len(await repo.list_by_campaign(cid)) == 1
    await store.set_turn_status(cid, turn.id, active=False)
    assert await repo.list_by_campaign(cid) == []
    assert (await repo.get_by_id(event.event_id)).status == "reverted"


@pytest.mark.asyncio
async def test_scribe_repeating_exact_executor_event_does_not_duplicate_transfer(db_session):
    from app.db.repositories.event_repo import EventRepository
    from app.db.repositories.turn_repo import TurnRepository
    from app.models.event import EventCreate
    from app.models.proposed_change import ChangeType
    from app.models.turn import TurnCreate
    from app.services.canon_applier import CanonApplier
    cid, _, _, _, _, _, scene = await setup_handoff(db_session)
    turns = TurnRepository(db_session)
    user = await turns.create(cid, TurnCreate(role="user", content="Передаю письмо", scene_id=scene))
    assistant = await turns.create(cid, TurnCreate(
        role="assistant", content="Письмо передано.", scene_id=scene, parent_turn_id=user.id,
    ))
    repo = EventRepository(db_session)
    await repo.create(cid, EventCreate(event_type="item_transfer", description="Письмо передано."),
                      source_turns=[user.id])
    await CanonApplier(db_session).apply(
        cid, ChangeType.EVENT, {"event_type": "item_transfer", "description": "Письмо передано."},
        assistant.id,
    )
    assert len(await repo.list_by_campaign(cid)) == 1


@pytest.mark.asyncio
async def test_bad_chatgpt_credentials_reach_session_zero_as_structured_reconnect_error(
    db_session, monkeypatch,
):
    from unittest.mock import AsyncMock

    from fastapi import HTTPException

    from app.api.session_zero import _run_interview_turn
    from app.providers.llm_provider import LLMProviderAuthenticationError
    from app.services.chatgpt_auth_service import ChatGPTAuthError
    from app.services.role_model_router import ModelRole, RoleModelRouter
    message = "Проверьте ключ шифрования или переподключите аккаунт."
    def fail(_self):
        raise ChatGPTAuthError(message)
    monkeypatch.setattr("app.services.role_model_router.ChatGPTAuthService.get_access_token", fail)
    with pytest.raises(LLMProviderAuthenticationError) as error:
        await RoleModelRouter(db_session).resolve(
            uuid4(), ModelRole.SESSION_ZERO, primary_config=SimpleNamespace(provider_kind="chatgpt"),
        )
    interview = SimpleNamespace(
        answer=AsyncMock(side_effect=error.value), is_rate_limited_error=lambda _exc: False,
    )
    with pytest.raises(HTTPException) as response:
        await _run_interview_turn(uuid4(), interview, message="Начинаем")
    assert response.value.status_code == 502
    assert response.value.detail["code"] == "provider_auth_required"
    assert response.value.detail["message"] == message
    assert response.value.detail["retryable"] is False
