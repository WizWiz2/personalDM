from app.services.narration_meta_guard import leaked_meta_lines


def test_narrator_does_not_publish_authorial_chronicle_meta_comment():
    assert leaked_meta_lines("Хроника не ждёт вдохновения.")
    assert not leaked_meta_lines("Хроника старого города сохранилась в архиве.")
