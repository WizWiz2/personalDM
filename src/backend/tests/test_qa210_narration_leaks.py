"""QA-210 publication safety: model scratchpad must never become story prose."""

from app.services.narration_meta_guard import leaked_meta_lines


def test_qa210_pipeline_placeholder_and_glyphs_rejected():
    for text in (
        "<|analysis|> Continue the response.",
        "[APPROVED TURN PLAN]",
        "Инес сказала: Иара \uF87F.",
    ):
        assert leaked_meta_lines(text), text


def test_legitimate_in_world_dialogue_is_not_filtered():
    for text in (
        "Инес заглянула в кассовую будку и назвала содержимое полки.",
        "— Ответ останется за тобой, — произнёс сторож.",
        "Жетон лежит на мокрых досках причала.",
    ):
        assert not leaked_meta_lines(text), text
