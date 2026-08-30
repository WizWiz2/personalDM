from app.models.session_zero_interview import SessionZeroInterviewModelDecision


def test_non_final_session_zero_reply_is_not_surgically_rewritten():
    message = "Хороший персонаж, запишем."
    decision = SessionZeroInterviewModelDecision.model_validate(
        {
            "assistant_message": message,
            "conversation_disposition": "continue",
            "tool_calls": [],
            "question_topics": [],
        }
    )

    assert decision.ready_to_finalize is False
    assert decision.conversation_disposition == "continue"
    assert decision.assistant_message == message


def test_existing_question_is_not_rewritten():
    message = "Отлично. А что он хочет получить от первой работы?"
    decision = SessionZeroInterviewModelDecision.model_validate(
        {
            "assistant_message": message,
            "conversation_disposition": "continue",
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
            "conversation_disposition": "start_game",
            "tool_calls": [{"name": "finalize_session_zero"}],
            "question_topics": [],
        }
    )

    assert decision.ready_to_finalize is True
    assert decision.conversation_disposition == "start_game"
    assert decision.assistant_message == message
