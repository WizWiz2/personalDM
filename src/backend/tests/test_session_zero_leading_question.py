from app.models.session_zero_interview import SessionZeroInterviewModelDecision


def test_non_final_session_zero_reply_always_keeps_conversation_open():
    decision = SessionZeroInterviewModelDecision.model_validate(
        {
            "assistant_message": "Хороший персонаж, запишем.",
            "tool_calls": [],
            "question_topics": [],
        }
    )

    assert decision.ready_to_finalize is False
    assert decision.assistant_message.startswith("Хороший персонаж, запишем.")
    assert "?" in decision.assistant_message


def test_existing_question_is_not_rewritten():
    message = "Отлично. А что он хочет получить от первой работы?"
    decision = SessionZeroInterviewModelDecision.model_validate(
        {
            "assistant_message": message,
            "tool_calls": [],
            "question_topics": ["character.first_goal"],
        }
    )

    assert decision.assistant_message == message


def test_finalize_reply_may_close_without_another_question():
    message = "Отлично, картина сложилась. Переходим к первой сцене."
    decision = SessionZeroInterviewModelDecision.model_validate(
        {
            "assistant_message": message,
            "tool_calls": [{"name": "finalize_session_zero"}],
            "question_topics": [],
        }
    )

    assert decision.ready_to_finalize is True
    assert decision.assistant_message == message
