import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.turn_repo import TurnRepository
from app.db.tables import EventParticipant
from app.db.truth_engine_table import TruthEventRecord
from app.models.character import CharacterCreate
from app.models.scene_development import SceneDevelopment, WorldSceneDevelopment, WorldStateUpdate
from app.models.turn import TurnCreate
from app.providers.llm_provider import LLMProviderError
from app.services.director_contract import DirectorContractReview, DirectorFulfillment, receipts, review_contract, validate_review
from app.services.published_world_state import PublishedWorldState
from app.services.scene_development import SceneDevelopmentService
from app.services.turn_undo_service import TurnUndoService
from tests.test_scene_development import world
from tests.test_turn_undo_authority import _base_turn

pytestmark = [pytest.mark.scene_development_enforced]


@pytest.mark.asyncio
async def test_director_missing_pressure_replans_before_freezing_narrator(db_session):
    authority, npc, absent, goal = await world(db_session)
    quiet = SceneDevelopment(disposition="quiet", actions=[], reason="Переход уже завершён.")
    beat = SceneDevelopment(disposition="act", actions=[], reason="Развивается существующий спор.",
        world_development=WorldSceneDevelopment(kind="complication", source_refs=[f"scene:{authority.target_scene_id}"],
            development="В споре появляется срок: решение нужно принять до закрытия дома.",
            player_opportunity="Можно договориться об отсрочке или принять решение.",
            progress_reason="У существующего конфликта появилась конкретная цена промедления."))
    generated = []

    async def generate(_provider, _selection, messages, **kwargs):
        model = kwargs["response_model"]
        generated.append(model)
        if model is SceneDevelopment:
            return (quiet if generated.count(model) == 1 else beat).model_dump(mode="json")
        assert model is DirectorContractReview
        repaired = generated.count(SceneDevelopment) == 2
        return {"items": [{"obligation_id": "D0", "status": "fulfilled" if repaired else "missing",
            "reason": "Срок даёт цену промедлению." if repaired else "Прибытие не продвинуло существующий конфликт.",
            "evidence_ref": "world" if repaired else None,
            "evidence_quote": beat.world_development.development if repaired else None}]}

    router = SimpleNamespace(resolve=AsyncMock(return_value=SimpleNamespace(config=SimpleNamespace(
        model_name="test", context_window=8192))), generate_json=AsyncMock(side_effect=generate))
    result, audit = await SceneDevelopmentService(db_session).plan(authority, router,
        director_policy={"obligations": ["Advance the existing conflict."]})
    assert result.world_development == beat.world_development
    assert generated == [SceneDevelopment, DirectorContractReview, SceneDevelopment, DirectorContractReview]
    assert audit["director_contract_review"][0]["items"][0]["status"] == "missing"
    assert audit["director_contract_review"][1]["items"][0]["status"] == "fulfilled"


def test_missing_receipt_or_unavailable_limitation_cannot_fulfill_contract():
    required = [{"id": "D0", "requirement": "Advance conflict."}]
    for item in [DirectorFulfillment(obligation_id="D0", status="fulfilled", evidence_ref="world",
                    evidence_quote="Вымышленное событие.", reason="Это якобы достаточное развитие."),
                 DirectorFulfillment(obligation_id="D0", status="deferred", limitation_refs=["pending_player_choice"],
                    reason="Якобы ожидается решение игрока.")]:
        result = validate_review(DirectorContractReview(items=[item]), required,
                                 {"quiet": "Переход завершён."}, {"pending_player_choice": None})
        assert result[0]["status"] == "missing"


def test_filtered_world_event_in_quiet_reason_is_not_a_realized_effect():
    text = "Рассол смывает метки, а три удара обозначают направление."
    decision = SceneDevelopment(disposition="quiet", actions=[], reason=text)
    evidence = receipts({}, decision)
    review = DirectorContractReview(items=[DirectorFulfillment(obligation_id="D0", status="fulfilled",
        evidence_ref="quiet", evidence_quote=text, reason="Смывание меток якобы ужесточает цену.")])
    assert text not in evidence.values()
    assert validate_review(review, [{"id": "D0", "requirement": "Harden consequence."}], evidence, {})[0]["status"] == "missing"


def test_extra_review_prose_does_not_retry_or_substitute_for_a_receipt():
    review = DirectorContractReview.model_validate({"summary": "Дополнительное пояснение.", "items": [
        {"obligation_id": "D0", "status": "fulfilled", "reason": "Якобы выполнено новое действие.",
         "receipt": "Неподтверждённая дополнительная строка."}]})
    assert validate_review(review, [{"id": "D0", "requirement": "Advance conflict."}], {}, {})[0]["status"] == "missing"
    assert "receipt" not in review.model_dump()["items"][0]


def test_receipt_quote_alias_still_requires_matching_authorized_text():
    text = "Женщина убирает заслон после объяснения цели героини."
    review = DirectorContractReview.model_validate({"items": [{"obligation_id": "D0", "status": "fulfilled",
        "reason": "Существующее препятствие разрешено переговорами.", "evidence_ref": "outcome:0", "receipt": text}]})
    required = [{"id": "D0", "requirement": "Advance existing conflict."}]
    assert validate_review(review, required, {"outcome:0": text}, {})[0]["status"] == "fulfilled"
    assert validate_review(review, required, {"outcome:0": "Прежнее препятствие осталось."}, {})[0]["status"] == "missing"


def test_valid_receipt_reference_can_be_quoted_by_engine_without_another_model_request():
    text = "Женщина убирает заслон после объяснения цели героини."
    review = DirectorContractReview(items=[DirectorFulfillment(obligation_id="D0", status="fulfilled",
        evidence_ref="outcome:0", reason="Переговоры разрешили препятствие.")])
    checked = validate_review(review, [{"id": "D0", "requirement": "Advance conflict."}], {"outcome:0": text}, {})
    assert checked[0]["status"] == "fulfilled"
    assert checked[0]["evidence_quote"] == text
    assert checked[0]["quote_source"] == "engine_receipt"


@pytest.mark.asyncio
@pytest.mark.parametrize("status,expected_plans", [("deferred", 1), ("missing", 2)])
async def test_rest_deferral_and_failed_repair_are_bounded_and_visible(db_session, status, expected_plans):
    authority, npc, absent, goal = await world(db_session)
    authority.player_input = "Отдыхаю и прошу пока меня не тревожить."
    calls = []

    async def generate(_provider, _selection, messages, **kwargs):
        calls.append(kwargs["response_model"])
        if kwargs["response_model"] is SceneDevelopment:
            return {"disposition": "quiet", "actions": [], "reason": "Игрок отдыхает."}
        return {"items": [{"obligation_id": "D0", "status": status,
            "reason": "Игрок явно попросил дать время на отдых.",
            "limitation_refs": ["player_input"] if status == "deferred" else []}]}

    router = SimpleNamespace(resolve=AsyncMock(return_value=SimpleNamespace(config=SimpleNamespace(
        model_name="test", context_window=8192))), generate_json=AsyncMock(side_effect=generate))
    decision, audit = await SceneDevelopmentService(db_session).plan(authority, router,
        director_policy={"obligations": ["Advance conflict unless the player asks to rest."]})
    assert decision.disposition == "quiet"
    assert calls.count(SceneDevelopment) == expected_plans
    assert calls.count(DirectorContractReview) == expected_plans
    assert audit["director_contract_review"][-1]["items"][0]["status"] == status
    assert audit["director_contract_status"] == ("missing" if status == "missing" else "reviewed")


@pytest.mark.asyncio
async def test_executed_action_can_update_existing_condition_but_not_speech_claim(db_session):
    authority, npc, absent, goal = await world(db_session)
    state_id = uuid4()
    context = await SceneDevelopmentService(db_session).context(authority)
    context["resolved_turn"]["published_world_state"] = {"conditions": [
        {"state_id": str(state_id), "subject": "Сигнал из подвала", "value": "Продолжается."}]}
    text = "После отключения механизма сигнал из подвала прекратился."
    quiet = SceneDevelopment(disposition="quiet", reason="Механизм отключён действием героя.", actions=[],
        state_updates=[WorldStateUpdate(state_id=state_id, subject="Подмена названия", value="Прекратился.",
                                        evidence_quote=text)])
    # Merely appearing in prose or in a quoted NPC response does not authorize a state change.
    context["resolved_turn"]["observable_consequences"] = [text]
    result, _ = SceneDevelopmentService.sanitize(quiet, context)
    assert result.state_updates == []
    context["completed_world_outcomes"] = [text]
    result, _ = SceneDevelopmentService.sanitize(quiet, context)
    assert result.disposition == "quiet"
    assert result.state_updates[0].subject == "Сигнал из подвала"
    assert result.state_updates[0].value == "Прекратился."


@pytest.mark.asyncio
async def test_world_conditions_replay_supersession_and_undo_across_scenes(db_session):
    campaign_id, player, scene, user, assistant = await _base_turn(db_session)
    state_id = uuid4()
    from app.models.turn_authority import TurnAuthority

    def authority(trigger, text, value):
        return TurnAuthority(campaign_id=campaign_id, trigger_turn_id=trigger, player_character_id=player.id,
            player_input="Жду.", target_scene_id=scene.id,
            scene_development=SceneDevelopment(disposition="act", reason="Изменение звука.", actions=[],
                world_development=WorldSceneDevelopment(kind="complication", source_refs=[f"scene:{scene.id}"],
                    development=text, player_opportunity="Можно проверить источник звука.",
                    progress_reason="Состояние источника изменилось."), state_updates=[
                        WorldStateUpdate(state_id=state_id, subject="Сигнал из подвала", value=value,
                                         evidence_quote=text)]))

    service = SceneDevelopmentService(db_session)
    await service.publish(authority(user.id, "Сигнал из подвала прекратился и больше не слышен.", "Прекратился."), assistant.id)
    other = await EntityRepository(db_session).create_character(campaign_id, CharacterCreate(canonical_name="Свидетель"))
    original = (await db_session.execute(select(TruthEventRecord).where(
        TruthEventRecord.source_turn_id == str(user.id)))).scalar_one()
    db_session.add(EventParticipant(event_id=original.event_id, entity_id=str(other.id)))
    repo = TurnRepository(db_session)
    user2 = await repo.create(campaign_id, TurnCreate(role="user", content="Жду ещё.", scene_id=scene.id))
    assistant2 = await repo.create(campaign_id, TurnCreate(role="assistant", content="Сигнал возобновился.",
        parent_turn_id=user2.id, scene_id=scene.id, context_snapshot={}))
    await service.publish(authority(user2.id, "Из подвала снова раздался отчётливый сигнал.", "Возобновился."), assistant2.id)
    await db_session.commit()
    projection = PublishedWorldState(db_session)
    # Observer retains the current condition when leaving the origin location.
    current = await projection.project(campaign_id, observer_id=player.id, local_location_id=uuid4())
    assert len(current["conditions"]) == 1
    assert current["conditions"][0]["value"] == "Возобновился."
    assert await projection.project(uuid4()) == {"conditions": [], "legacy_changes": []}
    assert await projection.project(campaign_id, observer_id=uuid4()) == {"conditions": [], "legacy_changes": []}
    # Seeing only the older event does not make its superseded value current.
    assert await projection.project(campaign_id, observer_id=other.id) == {"conditions": [], "legacy_changes": []}
    assert await TurnUndoService(db_session).undo_last_pair(campaign_id)
    current = await projection.project(campaign_id)
    assert current["conditions"][0]["value"] == "Прекратился."


@pytest.mark.asyncio
async def test_unknown_state_identity_or_unpublished_quote_cannot_change_condition(db_session):
    authority, npc, absent, goal = await world(db_session)
    context = await SceneDevelopmentService(db_session).context(authority)
    text = "Свет за окнами дома погас, оставив двор в темноте."
    decision = SceneDevelopment(disposition="act", reason="Изменение освещения.", actions=[],
        world_development=WorldSceneDevelopment(kind="complication", source_refs=[f"scene:{authority.target_scene_id}"],
            development=text, player_opportunity="Можно остаться во дворе или войти в дом.",
            progress_reason="Дом больше не освещает двор."), state_updates=[
                WorldStateUpdate(state_id=uuid4(), subject="Чужой слот", value="Переписан.", evidence_quote=text),
                WorldStateUpdate(subject="Освещение двора", value="Темно.", evidence_quote="Несуществующая строка."),
                WorldStateUpdate(subject="Освещение двора", value="Темно.", evidence_quote=text)])
    sanitized, audit = SceneDevelopmentService.sanitize(decision, context)
    updates = sanitized.state_updates
    assert len(updates) == 1 and updates[0].state_id is None
    # The accepted rendering contract carries current state and the authorized replacement.
    projected = authority.model_copy(update={"scene_development": sanitized}).narrator_payload()
    assert projected["scene_development"]["state_updates"][0]["value"] == "Темно."
    assert "evidence_quote" not in json.dumps(projected["scene_development"]["state_updates"])


@pytest.mark.asyncio
async def test_new_condition_is_reviewed_without_director_and_id_assigned_after_repair(db_session):
    authority, npc, absent, goal = await world(db_session)
    text = "Свет за окнами дома погас, оставив двор в темноте."
    decision = SceneDevelopment(disposition="act", reason="Изменение освещения.", actions=[],
        world_development=WorldSceneDevelopment(kind="complication", source_refs=[f"scene:{authority.target_scene_id}"],
            development=text, player_opportunity="Можно остаться во дворе или войти в дом.",
            progress_reason="Дом больше не освещает двор."), state_updates=[
                WorldStateUpdate(subject="Освещение двора", value="Темно.", evidence_quote=text)])
    calls = []

    async def generate(_provider, _selection, messages, **kwargs):
        calls.append(kwargs["response_model"])
        data = json.loads(messages[1].content)
        if kwargs["response_model"] is SceneDevelopment:
            if len(calls) > 1:
                assert data["previous_decision"]["state_updates"][0]["state_id"] is None
            return decision.model_dump(mode="json")
        assert data["decision"]["state_updates"][0]["state_id"] is None
        assert "reason" not in data["decision"]
        return {"items": [], "continuity_errors": ["Первый черновик требует уточнения основания."]
                if calls.count(DirectorContractReview) == 1 else []}

    router = SimpleNamespace(resolve=AsyncMock(return_value=SimpleNamespace(config=SimpleNamespace(
        model_name="test", context_window=8192))), generate_json=AsyncMock(side_effect=generate))
    result, audit = await SceneDevelopmentService(db_session).plan(authority, router)
    assert calls == [SceneDevelopment, DirectorContractReview, SceneDevelopment, DirectorContractReview]
    assert result.state_updates[0].state_id is not None
    assert audit["director_contract_status"] == "reviewed"


@pytest.mark.asyncio
async def test_auxiliary_contract_failure_keeps_resolved_turn_and_reports_unknown(db_session):
    authority, npc, absent, goal = await world(db_session)
    calls = []

    async def generate(_provider, _selection, messages, **kwargs):
        calls.append(kwargs["response_model"])
        if kwargs["response_model"] is SceneDevelopment:
            return {"disposition": "quiet", "reason": "Выполненный ход сохранён.", "actions": []}
        raise LLMProviderError("Model returned invalid auxiliary JSON")

    router = SimpleNamespace(resolve=AsyncMock(return_value=SimpleNamespace(config=SimpleNamespace(
        model_name="test", context_window=8192))), generate_json=AsyncMock(side_effect=generate))
    result, audit = await SceneDevelopmentService(db_session).plan(authority, router,
        director_policy={"obligations": ["Advance the existing conflict."]})
    assert result.disposition == "quiet"
    assert calls == [SceneDevelopment, DirectorContractReview]
    assert audit["director_contract_status"] == "unavailable"
    assert audit["director_contract_review"][0]["items"] == []


@pytest.mark.asyncio
async def test_reviewer_receives_previous_published_initiative_for_newness():
    prior = {"disposition": "act", "actions": [{"actor_id": str(uuid4()),
        "action": "Женщина заслоняет выход и спрашивает, что нужно героине."}]}
    context = {"player_input": "Почему ты перегородила проход?", "recent_developments": [prior],
        "resolved_turn": {"observable_consequences": ["Она объясняет, что ждёт ответа о цели прохода."]},
        "director_policy": {"obligations": ["Create a sudden playable turn."]}}
    decision = SceneDevelopment(disposition="quiet", actions=[], reason="Повторено прежнее требование.")

    async def generate(_provider, _selection, messages, **kwargs):
        data = json.loads(messages[1].content)
        assert data["recent_developments"] == [prior]
        assert "Repeating an existing blockade" in messages[0].content
        return {"items": [{"obligation_id": "D0", "status": "missing",
                          "reason": "Повтор условия прохода не изменил ставок."}]}

    checked = await review_contract(SimpleNamespace(generate_json=generate), None, context, decision)
    assert checked[0]["status"] == "missing"
