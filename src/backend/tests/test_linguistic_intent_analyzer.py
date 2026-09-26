from app.services.linguistic_intent_analyzer import LinguisticIntentAnalyzer


def test_russian_imperative_is_owned_by_addressee() -> None:
    analysis = LinguisticIntentAnalyzer().analyze(
        "Раздевайся. — Говорю я.",
        ["Раздевайся"],
    )

    assert analysis.action_roles == ("addressee",)
    assert analysis.imperative_clauses == ("Раздевайся.",)
    assert analysis.information_request_only is None


def test_third_person_state_questions_are_for_narrator() -> None:
    analysis = LinguisticIntentAnalyzer().analyze(
        "В чем сейчас Мария? Она разделась до гола?"
    )

    assert analysis.information_request_only is True
    assert analysis.information_recipient == "narrator"


def test_second_person_question_is_for_character() -> None:
    analysis = LinguisticIntentAnalyzer().analyze("Мария, ты уже готова?")

    assert analysis.information_request_only is True
    assert analysis.information_recipient == "character"


def test_first_person_statement_is_owned_by_speaker() -> None:
    analysis = LinguisticIntentAnalyzer().analyze("Я открываю дверь.")

    assert analysis.uniform_action_role == "speaker"
    assert analysis.information_request_only is None
