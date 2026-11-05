from app.models.turn import ChatMessage
from app.services.planning_context import outcome_reference_context


def test_style_and_transient_texture_are_not_blocking_evidence():
    context = outcome_reference_context([ChatMessage(role="system", content=(
        "[BEGIN SESSION ZERO CONTRACT]\nГраницы: без пыток.\n[END SESSION ZERO CONTRACT]\n"
        "[GAME MASTER PERSONA — style only, not world facts]\nVoice: суровый.\n"
        "[Current Scene: Архив]\nДверь заперта на засов.\n"
        "[Progress Watchdog] Нужна новая угроза.\n"
        "[Campaign Facts & History]\nМария отказалась отдавать ключ.\n"
        "[STRUCTURED ACTION REFERENCES]\nPlayer-owned items: письмо [id=item-id]\n"
        "Planner inventory contract:\n- take requires an object.\n"
        "[Recent Scene Texture — transient, non-canon]\nТень похожа на охранника.\n"
    ))])
    assert "Дверь заперта на засов." in context
    assert "Мария отказалась отдавать ключ." in context
    assert "Границы: без пыток." in context
    assert "письмо [id=item-id]" in context
    assert "Voice:" not in context
    assert "Нужна новая угроза." not in context
    assert "Тень похожа на охранника." not in context
    assert "take requires" not in context


def test_outcomes_keep_typed_item_evidence_without_a_duplicate_item_index():
    snapshot = '{"item_id":"123","name":"Письмо","quantity":2}'
    message = ChatMessage(role="system", content=(
        "Registered items: Письмо [id=123]\n"
        "[CURRENT ITEM POSITIONS — executor-owned, override old prose]\n"
        + snapshot
    ))
    assert snapshot in outcome_reference_context([message])
    assert "Письмо [id=123]" not in outcome_reference_context([message])

    legacy = ChatMessage(role="system", content="Registered items: Письмо [id=123]")
    assert "Письмо [id=123]" in outcome_reference_context([legacy])
