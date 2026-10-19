from app.services.narration_meta_guard import leaked_meta_lines


def test_qa213_chronicle_meta_sentence_does_not_pass_as_story():
    text = "Хроника не ждёт вдохновения."
    assert leaked_meta_lines(text) == [text]


def test_normal_chronicle_world_detail_is_not_rejected():
    assert leaked_meta_lines("На столе лежит древняя хроника, раскрытая на середине.") == []
