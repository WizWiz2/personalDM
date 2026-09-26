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
