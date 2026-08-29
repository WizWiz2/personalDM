from uuid import uuid4

from app.models.narration_validation import NarrationValidationResult
from app.models.turn_authority import TurnAuthority
from app.services.turn_authority_planner import TurnAuthorityPlanner
from app.services.turn_authority_validator import TurnAuthorityValidator


def _pass() -> NarrationValidationResult:
    return NarrationValidationResult(verdict="pass", summary="Кандидат принят.", violations=[])


def _authority(player_input: str) -> TurnAuthority:
    return TurnAuthority(
        campaign_id=uuid4(),
        trigger_turn_id=uuid4(),
        player_character_name="Рэт Уайтмоур",
        player_input=player_input,
        observable_consequences=["У двери остаётся тусклый свет."],
    )


def test_semantic_plan_reviewer_owns_unresolved_choice_stale_turn_and_movement_meaning():
    prompt = TurnAuthorityPlanner.SEMANTIC_REVIEW_PROMPT

    assert "CURRENT INPUT" in prompt
    assert "PLAYER AGENCY" in prompt
    assert "MOVEMENT/TIME" in prompt
    assert "CONTACT/IDENTITY" in prompt
    assert "LANGUAGE" in prompt
    assert "Do not use keyword lists" in prompt


def test_chinese_narration_is_rejected_even_when_model_validator_passes():
    authority = _authority("Я отмечаю тусклый свет из-под двери.")
    candidate = "微弱的光线从第三扇门下透出，Rat_Whitemour决定进一步调查。"

    result = TurnAuthorityValidator.apply_deterministic_language(_pass(), authority, candidate)

    assert result.verdict == "repair_required"
    assert any(item.violation_type == "other" for item in result.violations)


def test_russian_narration_may_keep_latin_canonical_name():
    authority = _authority("Я осматриваю кабинет.")
    candidate = "Rat_Whitemour замечает на столе слой пыли и закрытую чернильницу."

    result = TurnAuthorityValidator.apply_deterministic_language(_pass(), authority, candidate)

    assert result.verdict == "pass"


def test_player_agency_is_semantic_validator_territory_not_deterministic_word_matching():
    prompt = TurnAuthorityValidator.SYSTEM_PROMPT
    authority = _authority("Я решаю: войти внутрь или сначала позвать чиновника.")
    candidate = "Рэт Уайтмоур входит в комнату и обещает вернуться с полицией."

    deterministic = TurnAuthorityValidator.apply_deterministic_player_agency(
        _pass(), authority, candidate
    )

    assert deterministic.verdict == "pass"
    assert "PLAYER AGENCY" in prompt
    assert "grammatical subject" in prompt
    assert "Never decide from" in prompt


def test_validator_contract_protects_supplied_dialogue_without_lexical_echo_guard():
    prompt = TurnAuthorityValidator.SYSTEM_PROMPT

    assert "new voluntary dialogue" in prompt
    assert "player_input" in prompt
    assert "shortest exact offending fragment" in prompt
    assert "NPC-owned fragment as player agency" in prompt
