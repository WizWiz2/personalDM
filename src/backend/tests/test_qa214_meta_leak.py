from app.services.narration_meta_guard import leaked_meta_lines


def test_chronicle_wording_alone_is_not_a_protocol_marker():
    assert not leaked_meta_lines("Хроника не ждёт вдохновения.")
    assert not leaked_meta_lines("Хроника старого города сохранилась в архиве.")
