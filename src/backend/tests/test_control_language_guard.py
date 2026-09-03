from app.services.control_language_guard import (
    CONTROL_LANGUAGE_CONTRACT,
    MOVEMENT_SCOPE_CONTRACT,
    review_language_mismatch,
)
from app.services.turn_authority_planner import SemanticPlanReview


def test_chinese_semantic_review_is_rejected_for_russian_input():
    review = SemanticPlanReview(
        verdict="repair_required",
        summary="需要修复",
        issues=[
            "MOVEMENT/TIME: 计划未使用对应的结构化过渡，尽管人类指令涉及物理动作。"
        ],
    )

    assert review_language_mismatch(
        review,
        "Я аккуратно кладу латунный ключ на рабочий стол и убираю руку.",
    )


def test_russian_semantic_review_matches_russian_input():
    review = SemanticPlanReview(
        verdict="repair_required",
        summary="План требует исправления.",
        issues=["Предмет нужно обработать как inventory place в текущей сцене."],
    )

    assert not review_language_mismatch(
        review,
        "Я аккуратно кладу латунный ключ на рабочий стол и убираю руку.",
    )


def test_control_language_contract_is_explicitly_russian_for_russian_input():
    assert "latest human input is Russian" in CONTROL_LANGUAGE_CONTRACT
    assert "MUST be Russian" in CONTROL_LANGUAGE_CONTRACT
    assert "Chinese" in CONTROL_LANGUAGE_CONTRACT


def test_movement_scope_distinguishes_item_motion_from_scene_transition():
    assert "placing an owned key on a table" in MOVEMENT_SCOPE_CONTRACT
    assert "not a location or time transition" in MOVEMENT_SCOPE_CONTRACT
    assert "кладу латунный ключ на стол" in MOVEMENT_SCOPE_CONTRACT
