import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.db.repositories.scene_repo import SceneRepository
from app.db.repositories.turn_repo import TurnRepository
from app.db.tables import SceneThesis, Turn
from app.models.narration_validation import NarrationValidationResult, NarrationViolation
from app.models.scene_development import SceneDevelopment, WorldSceneDevelopment
from app.models.scene_thesis import SceneThesisCreate, ThesisType
from app.models.turn import ChatMessage, TurnCreate
from app.models.turn_authority import TurnAuthority
from app.services.narrator_quality_recovery_guard import compact_narrator_payload, install
from app.services.narration_publication_guard import NarrationPublicationGuard
from app.services.thesis_curator import CuratorResponse, ThesisCurator, ThesisResolution
from app.services.turn_saga import TurnSaga
from app.services.turn_undo_service import TurnUndoService
from tests.test_turn_undo_authority import _base_turn


@pytest.mark.parametrize("label", ["scene_development", "incomplete_turn", "new_model_category"])
def test_unknown_review_label_preserves_error_without_a_model_retry(label):
    violation = NarrationViolation(violation_type=label, evidence="пропущен результат",
                                   correction="Сохранить утверждённый результат.")
    assert violation.violation_type == "other"
    assert violation.severity == "error"
    assert NarrationValidationResult(verdict="repair_required", violations=[violation]).violations == [violation]
    with pytest.raises(ValidationError):
        NarrationValidationResult(verdict="pass", violations=[violation])


def test_installed_compact_contract_keeps_approved_world_change_and_agency():
    install()
    world = WorldSceneDevelopment(kind="complication", source_refs=["private:source"],
        development="Стук обрывается, и в щели проступает бело-синяя метка.",
        player_opportunity="Можно осмотреть щель или продолжить путь.",
        progress_reason="Приватная причина выбора события.")
    authority = TurnAuthority(campaign_id=uuid4(), trigger_turn_id=uuid4(),
        player_input="Осматриваю щель.", player_character_name="Лада",
        present_character_names=["Лада"], observable_consequences=["Соляной след уходит в щель."],
        protected_player_decisions=["Следующий путь выбирает игрок."],
        scene_development=SceneDevelopment(disposition="act", reason="Приватный замысел.",
                                           actions=[], world_development=world))
    payload = compact_narrator_payload(authority)
    assert payload["scene_development"]["world_development"]["development"] == world.development
    assert payload["protected_player_decisions"] == authority.protected_player_decisions
    messages = TurnSaga._inject_authority(object(), [ChatMessage(role="system", content="Кампания")], authority)
    assert world.development in messages[0].content
    assert world.player_opportunity in messages[0].content
    assert "private:source" not in messages[0].content
    assert world.progress_reason not in messages[0].content
    assert "Приватный замысел" not in messages[0].content


def test_validated_dialogue_with_a_colon_keeps_its_literary_surface():
    authority = TurnAuthority(campaign_id=uuid4(), trigger_turn_id=uuid4(),
        player_input="Спрашиваю о проходе.", present_character_names=["Лада", "Иван"],
        observable_consequences=["Иван сообщает, что прямого доступа нет."])
    text = "Иван остаётся у колодца.\n\n— Прямого доступа нет: щель ведёт под мостовую."
    published, audit = NarrationPublicationGuard.publish(authority, text,
                                                        NarrationValidationResult(verdict="pass"))
    assert published == text
    assert audit["mode"] == "validated_candidate"
    assert NarrationPublicationGuard._player_facing_fragment("Осмотр — completed: щель найдена.") is None


@pytest.mark.asyncio
async def test_curator_result_references_exclude_future_and_undone_turns(db_session):
    campaign_id, player, scene, user, assistant = await _base_turn(db_session)
    row = await db_session.get(Turn, str(assistant.id))
    row.context_snapshot = json.dumps({"turn_authority": {"observable_consequences": ["Проход найден."]}})
    future = await TurnRepository(db_session).create(campaign_id, TurnCreate(role="assistant",
        scene_id=scene.id, parent_turn_id=user.id, content="Позднейшее событие.",
        context_snapshot={"turn_authority": {"observable_consequences": ["Будущее событие."]}}))
    evidence = await ThesisCurator(db_session)._published_result_evidence(campaign_id, scene.id, assistant.id)
    assert [item["result"] for item in evidence.values()] == ["Проход найден."]
    row.status = "undone"
    await db_session.flush()
    assert await ThesisCurator(db_session)._published_result_evidence(campaign_id, scene.id, assistant.id) == {}
    assert future.id != assistant.id


@pytest.mark.asyncio
async def test_evidenced_thread_resolution_is_undoable(db_session):
    campaign_id, player, scene, user, assistant = await _base_turn(db_session)
    old = await SceneRepository(db_session).create_thesis(scene.id, SceneThesisCreate(
        thesis_type=ThesisType.UNRESOLVED_BEAT, text="Путь за торговыми рядами пока не найден."))
    pinned = await SceneRepository(db_session).create_thesis(scene.id, SceneThesisCreate(
        thesis_type=ThesisType.CANON, text="Город стоит на берегу моря.", pinned=True))
    envelope = CuratorResponse(resolutions=[
        ThesisResolution(thesis_id=old.id, evidence_ref="R1", reason="Найденный проход завершает поиск пути."),
        ThesisResolution(thesis_id=pinned.id, evidence_ref="R1", reason="Ошибочное закрытие постоянного канона."),
        ThesisResolution(thesis_id=uuid4(), evidence_ref="R1", reason="Ошибочное закрытие чужой нити."),
        ThesisResolution(thesis_id=old.id, evidence_ref="R404", reason="Несуществующее основание для закрытия."),
    ])
    ids = ThesisCurator._evidenced_resolutions(envelope, [old, pinned], {"R1": {"result": "Проход найден."}})
    assert ids == {old.id}
    await ThesisCurator(db_session).reconcile(scene.id, assistant.id, [], resolve_thesis_ids=ids)
    await db_session.commit()
    stored = await db_session.get(SceneThesis, str(old.id))
    assert stored.status == "resolved"
    assert await TurnUndoService(db_session).undo_last_pair(campaign_id)
    await db_session.refresh(stored)
    assert stored.status == "active"
