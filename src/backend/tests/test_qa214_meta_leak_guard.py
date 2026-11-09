from app.services.narration_meta_guard import leaked_meta_lines


def test_chronicle_sentence_can_belong_to_the_story():
    text = "Хроника не ждёт вдохновения."
    assert leaked_meta_lines(text) == []


def test_normal_chronicle_world_detail_is_not_rejected():
    assert leaked_meta_lines("На столе лежит древняя хроника, раскрытая на середине.") == []
