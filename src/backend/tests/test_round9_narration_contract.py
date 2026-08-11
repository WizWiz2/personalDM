from uuid import uuid4

from app.models.narration_validation import NarrationValidationResult
from app.models.turn_authority import PlannedNpcIntroduction, TurnAuthority
from app.services.turn_authority_planner import CoordinatedTurnPlan, TurnAuthorityPlanner
from app.services.turn_authority_validator import TurnAuthorityValidator
from app.services.turn_planner import NarrationPolicy, SceneTransitionPlan


def _pass() -> NarrationValidationResult:
    return NarrationValidationResult(
        verdict="pass",
        summary="Кандидат принят.",
        violations=[],
    )


def _authority(player_input: str) -> TurnAuthority:
    return TurnAuthority(
        campaign_id=uuid4(),
        trigger_turn_id=uuid4(),
        player_character_name="Рэт Уайтмоур",
        player_input=player_input,
        observable_consequences=["У двери остаётся тусклый свет."],
    )


def test_unresolved_choice_cannot_choose_transition_or_spawn_contact():
    plan = CoordinatedTurnPlan(
        player_intent="Рэт решает войти в архив.",
        resolution="transition",
        scene_transition=SceneTransitionPlan(
            required=True,
            transition_type="location_transition",
            destination_location="Архив",
        ),
        narration_policy=NarrationPolicy(),
        npc_introductions=[
            PlannedNpcIntroduction(
                canonical_name="Молодой чиновник",
                role="чиновник",
                temporary_name=True,
                reason="Отвечает на стук.",
            )
        ],
    )

    issues = TurnAuthorityPlanner.contract_issues(
        plan,
        "Я замираю у двери — войти или постучать? Решаю на месте.",
    )

    assert any("pending_player_choice" in issue for issue in issues)
    assert any("cannot complete a location transition" in issue for issue in issues)
    assert any("cannot introduce a new NPC" in issue for issue in issues)
    assert not any("direct contact is unresolved" in issue for issue in issues)


def test_planner_rejects_stale_previous_turn_intent():
    plan = CoordinatedTurnPlan(
        player_intent="Осторожно приоткрыть дверь и заглянуть в тёмную комнату.",
        resolution="observation",
    )

    issues = TurnAuthorityPlanner.contract_issues(
        plan,
        "Я проверяю запись: в Ратуше висит герб города — золотой лев на чёрном поле.",
    )

    assert any("latest player input" in issue for issue in issues)


def test_russian_turn_rejects_chinese_structured_names_before_authority():
    plan = CoordinatedTurnPlan(
        player_intent="Подняться на второй этаж Ратуши.",
        resolution="transition",
        scene_transition=SceneTransitionPlan(
            required=True,
            transition_type="location_transition",
            destination_location="Rat_House 第二层 档案室",
            scene_title="第二层档案室",
        ),
    )

    issues = TurnAuthorityPlanner.contract_issues(
        plan,
        "Я решительно направляюсь к лестнице, ведущей на второй этаж Ратуши.",
    )

    assert any("Russian player input" in issue for issue in issues)


def test_chinese_narration_is_rejected_even_when_model_validator_passes():
    authority = _authority("Я отмечаю тусклый свет из-под двери.")
    candidate = "微弱的光线从第三扇门下透出，Rat_Whitemour决定进一步调查。"

    result = TurnAuthorityValidator.apply_deterministic_language(
        _pass(),
        authority,
        candidate,
    )

    assert result.verdict == "repair_required"
    assert any(item.violation_type == "other" for item in result.violations)


def test_russian_narration_may_keep_latin_canonical_name():
    authority = _authority("Я осматриваю кабинет.")
    candidate = "Rat_Whitemour замечает на столе слой пыли и закрытую чернильницу."

    result = TurnAuthorityValidator.apply_deterministic_language(
        _pass(),
        authority,
        candidate,
    )

    assert result.verdict == "pass"


def test_narrator_cannot_invent_new_quoted_player_speech():
    authority = _authority(
        "Я решаю: войти внутрь и представиться или сначала позвать чиновника."
    )
    candidate = (
        "Рэт Уайтмоур стучит и говорит: «Я частный детектив. Есть здесь кто-нибудь?»"
    )

    result = TurnAuthorityValidator.apply_deterministic_player_agency(
        _pass(),
        authority,
        candidate,
    )

    assert result.verdict == "repair_required"
    assert any(item.violation_type == "player_agency" for item in result.violations)


def test_narrator_cannot_complete_branch_player_left_unresolved():
    authority = _authority(
        "Я делаю что-то неопределённое — может, стою и жду, может, иду к двери."
    )
    candidate = "Рэт Уайтмоур входит в тёмную комнату и осматривается."

    result = TurnAuthorityValidator.apply_deterministic_player_agency(
        _pass(),
        authority,
        candidate,
    )

    assert result.verdict == "repair_required"
    assert any(item.violation_type == "player_agency" for item in result.violations)


def test_supplied_player_dialogue_is_not_rejected_as_invented():
    authority = _authority("Я говорю: «Я частный детектив. Кто здесь работает?»")
    candidate = "Рэт Уайтмоур говорит: «Я частный детектив. Кто здесь работает?»"

    result = TurnAuthorityValidator.apply_deterministic_player_agency(
        _pass(),
        authority,
        candidate,
    )

    assert result.verdict == "pass"
