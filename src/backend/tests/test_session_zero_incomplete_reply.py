from app.services.session_zero_agent import SessionZeroAgent


def test_assistant_message_incomplete_detects_mid_sentence_cutoff():
    assert SessionZeroAgent._assistant_message_incomplete(
        "какой именно аспект этой"
    )
    assert SessionZeroAgent._assistant_message_incomplete("Если хочешь, могу предложить")
    assert SessionZeroAgent._assistant_message_incomplete("Хорошо,")


def test_assistant_message_incomplete_allows_finished_russian_lines():
    assert not SessionZeroAgent._assistant_message_incomplete(
        "Во что тебе хочется сыграть?"
    )
    assert not SessionZeroAgent._assistant_message_incomplete("Начнём с мира.")
    assert not SessionZeroAgent._assistant_message_incomplete("Стоп…")
    assert not SessionZeroAgent._assistant_message_incomplete("")
