from app.models.session_zero_interview import SessionZeroInterviewModelDecision


def test_question_alias_preserves_patch_and_message():
    decision = SessionZeroInterviewModelDecision.model_validate(
        {
            "patch": {
                "world": {
                    "setting_name": "Shadowrun",
                    "genre": "киберпанк с магией",
                }
            },
            "question": "Какие черты Shadowrun особенно важны для этой кампании?",
            "question_topics": ["world.world_summary"],
        }
    )

    assert decision.assistant_message.startswith("Какие черты Shadowrun")
    assert decision.patch.world.setting_name == "Shadowrun"
    assert decision.patch.world.genre == "киберпанк с магией"
    assert decision.question_topics == ["world.world_summary"]


def test_legacy_draft_and_next_question_are_normalized_together():
    decision = SessionZeroInterviewModelDecision.model_validate(
        {
            "draft": {"character": {"name": "Кабуто"}},
            "next_question": "Кто такой Кабуто и чем он занимается?",
            "question_topics": ["character.description"],
        }
    )

    assert decision.assistant_message == "Кто такой Кабуто и чем он занимается?"
    assert decision.patch.character.name == "Кабуто"


def test_missing_message_gets_safe_nonempty_fallback():
    decision = SessionZeroInterviewModelDecision.model_validate(
        {
            "patch": {"world": {"setting_name": "Shadowrun"}},
            "question_topics": ["world.genre"],
        }
    )

    assert decision.assistant_message.strip()
    assert decision.assistant_message != "Продолжим нулевую сессию."
    assert "сохранил" in decision.assistant_message.casefold()
    assert decision.patch.world.setting_name == "Shadowrun"
