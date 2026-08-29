from app.models.session_zero_interview import (
    SessionZeroInterviewDraft,
    SessionZeroInterviewPatch,
    SessionZeroStarterNPC,
)
from app.models.turn import ChatMessage
from app.models.turn_authority import PlannedNpcIntroduction
from app.services.session_zero_interview import SessionZeroInterviewService
from app.services.starter_identity import (
    present_character_names,
    reconcile_starter_npcs,
    sanitize_existing_present_npc_introductions,
)
from app.services.turn_authority_planner import CoordinatedTurnPlan, TurnAuthorityPlanner


def _starter(*, role="Посетительница", name=None, description=None, reason=None):
    return SessionZeroStarterNPC(
        role=role,
        name=name,
        description=description,
        reason=reason,
        present_at_start=True,
    )


def test_alternating_role_and_name_starter_specs_collapse_to_one_identity():
    specs = [
        _starter(description="Взволнованная свидетельница ждёт разговора."),
        _starter(name="Анна", description="Свидетельница по текущему делу."),
        _starter(description="Посетительница всё ещё находится в кабинете."),
        _starter(name="Анна", reason="Пришла сообщить о происшествии."),
        _starter(description="Посетительница физически присутствует в первой сцене."),
    ]

    reconciled = reconcile_starter_npcs(specs)

    assert len(reconciled) == 1
    assert reconciled[0].name == "Анна"
    assert reconciled[0].role == "Посетительница"
    assert reconciled[0].present_at_start is True


def test_session_zero_patch_accumulation_keeps_one_starter_identity():
    draft = SessionZeroInterviewDraft()

    draft = SessionZeroInterviewService._apply_patch(
        draft,
        SessionZeroInterviewPatch.model_validate(
            {
                "world": {
                    "starter_npcs": [
                        {
                            "role": "Посетительница",
                            "description": "Взволнованная свидетельница ждёт разговора.",
                        }
                    ]
                }
            }
        ),
        explicit_correction=False,
    )
    draft = SessionZeroInterviewService._apply_patch(
        draft,
        SessionZeroInterviewPatch.model_validate(
            {
                "world": {
                    "starter_npcs": [
                        {
                            "role": "Посетительница",
                            "name": "Анна",
                            "reason": "Пришла сообщить о происшествии.",
                        }
                    ]
                }
            }
        ),
        explicit_correction=False,
    )
    draft = SessionZeroInterviewService._apply_patch(
        draft,
        SessionZeroInterviewPatch.model_validate(
            {
                "world": {
                    "starter_npcs": [
                        {
                            "role": "Посетительница",
                            "description": "Посетительница всё ещё находится в кабинете.",
                        }
                    ]
                }
            }
        ),
        explicit_correction=False,
    )

    assert len(draft.world.starter_npcs) == 1
    assert draft.world.starter_npcs[0].name == "Анна"
    assert SessionZeroInterviewService.summary(draft).count("Анна") == 1
    assert "Посетительница; Анна" not in SessionZeroInterviewService.summary(draft)


def test_role_name_and_specific_name_collapse_to_one_innkeeper():
    reconciled = reconcile_starter_npcs(
        [
            _starter(role="трактирщик", name="Хозяин"),
            _starter(role="трактирщик", name="Хозяин трактира"),
        ]
    )

    assert len(reconciled) == 1
    assert reconciled[0].name == "Хозяин трактира"


def test_present_innkeeper_is_not_reintroduced_under_role_name():
    plan = _conversation_plan("Хозяин", "трактирщик")

    sanitize_existing_present_npc_introductions(plan, {"Вера", "Хозяин трактира"})

    assert plan.npc_introductions == []


def test_two_different_named_starters_with_same_role_never_merge():
    reconciled = reconcile_starter_npcs(
        [
            _starter(name="Анна"),
            _starter(name="Мария"),
        ]
    )

    assert len(reconciled) == 2
    assert {item.name for item in reconciled} == {"Анна", "Мария"}


def test_ambiguous_generic_entry_stays_separate_when_role_has_two_named_people():
    reconciled = reconcile_starter_npcs(
        [
            _starter(name="Анна"),
            _starter(name="Мария"),
            _starter(description="Одна из посетительниц ждёт у двери."),
        ]
    )

    assert len(reconciled) == 3
    assert sum(item.name is None for item in reconciled) == 1


def test_authoritative_scene_state_parser_reads_exact_present_character_names():
    messages = [
        ChatMessage(
            role="system",
            content=(
                "[AUTHORITATIVE SCENE STATE]\n"
                "Scene: Кабинет (active)\n"
                "Physically present characters: Анна, Марк\n"
                "Objects physically here: Фотография\n"
            ),
        )
    ]

    assert present_character_names(messages) == frozenset({"Анна", "Марк"})


def _conversation_plan(name: str, role: str) -> CoordinatedTurnPlan:
    return CoordinatedTurnPlan(
        player_intent="поговорить с собеседником",
        resolution="conversation",
        npc_introductions=[
            PlannedNpcIntroduction(
                canonical_name=name,
                role=role,
                temporary_name=False,
                reason="Planner перечислил персонажа в текущем ходе.",
            )
        ],
    )


def test_existing_physically_present_npc_is_not_a_new_introduction():
    plan = _conversation_plan("Анна", "посетительница")

    sanitize_existing_present_npc_introductions(plan, {"Марк", "Анна"})

    assert plan.npc_introductions == []


def test_genuinely_new_unsolicited_npc_is_a_semantic_planner_failure():
    prompt = TurnAuthorityPlanner.SEMANTIC_REVIEW_PROMPT

    assert "CONTACT/IDENTITY" in prompt
    assert "unknown physical responder" in prompt
    assert "npc_introductions" in prompt


def test_explicit_unknown_contact_remains_supported_by_typed_npc_introduction():
    plan = _conversation_plan("Прохожий", "прохожий")

    sanitize_existing_present_npc_introductions(plan, {"Марк", "Анна"})

    assert len(plan.npc_introductions) == 1
    assert plan.npc_introductions[0].canonical_name == "Прохожий"
    assert "CONTACT/IDENTITY" in TurnAuthorityPlanner.SEMANTIC_REVIEW_PROMPT
