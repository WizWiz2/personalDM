from app.models.session_zero_interview import (
    SessionZeroInterviewDraft,
    SessionZeroInterviewPatch,
    SessionZeroStarterNPC,
)
from app.models.turn import ChatMessage
from app.models.turn_authority import PlannedNpcIntroduction
from app.runtime import install_runtime
from app.services.round33_identity_guard import (
    present_character_names,
    reconcile_starter_npcs,
    sanitize_existing_present_npc_introductions,
)
from app.services.session_zero_interview import SessionZeroInterviewService
from app.services.systemless_authority_guard import systemless_contract_issues
from app.services.turn_authority_planner import CoordinatedTurnPlan


def _starter(*, role="Посетительница", name=None, description=None, reason=None):
    return SessionZeroStarterNPC(
        role=role,
        name=name,
        description=description,
        reason=reason,
        present_at_start=True,
    )


def test_round33_alternating_role_and_name_starter_specs_collapse_to_one_identity():
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


def test_round33_session_zero_patch_accumulation_keeps_one_starter_identity():
    install_runtime()
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
    issues = systemless_contract_issues(plan, "Расскажите, что случилось.")

    assert plan.npc_introductions == []
    assert not any("new physical NPC introductions" in issue for issue in issues)


def test_genuinely_new_unsolicited_npc_still_fails_closed():
    plan = _conversation_plan("Незнакомец", "человек в коридоре")

    sanitize_existing_present_npc_introductions(plan, {"Марк", "Анна"})
    issues = systemless_contract_issues(plan, "Осматриваю фотографию на столе.")

    assert len(plan.npc_introductions) == 1
    assert any("new physical NPC introductions" in issue for issue in issues)


def test_explicit_unknown_contact_is_not_removed_by_present_npc_sanitizer():
    plan = _conversation_plan("Прохожий", "прохожий")

    sanitize_existing_present_npc_introductions(plan, {"Марк", "Анна"})
    issues = systemless_contract_issues(
        plan,
        "Расспрашиваю прохожего, не видел ли он ночью машину.",
    )

    assert len(plan.npc_introductions) == 1
    assert not any("new physical NPC introductions" in issue for issue in issues)
