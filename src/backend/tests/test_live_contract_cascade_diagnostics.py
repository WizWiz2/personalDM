from live_model_contracts.diagnostic_console_runner import (
    CASCADE_WARNING_REQUESTS,
    _attempt_count,
    _collapsed_trace,
    _planner_turns,
)


def test_attempt_count_prefers_explicit_attempt_count() -> None:
    assert _attempt_count({"attempt_count": 3, "attempts": [{}, {}]}) == 3
    assert _attempt_count({"attempts": [{}, {}]}) == 2
    assert _attempt_count({}) == 1


def test_collapsed_trace_preserves_order_and_structured_retries() -> None:
    records = [
        {
            "role": "planner",
            "response_model": "CoordinatedTurnPlan",
            "attempt_count": 1,
        },
        {
            "role": "planner",
            "response_model": "SemanticPlanReview",
            "attempt_count": 1,
        },
        {
            "role": "planner",
            "response_model": "PlanReviewAdjudication",
            "attempt_count": 2,
        },
        {
            "role": "narration_validator",
            "response_model": "NarrationValidationResult",
            "attempt_count": 1,
        },
    ]

    assert _collapsed_trace(records) == (
        "CoordinatedTurnPlan -> SemanticPlanReview -> PlanReviewAdjudication[x2]"
    )


def test_planner_turns_do_not_merge_two_user_turns_from_same_contract() -> None:
    records = [
        {
            "sequence": 1,
            "task_id": "turn-a",
            "role": "planner",
            "response_model": "CoordinatedTurnPlan",
            "attempt_count": 1,
            "wall_ms": 1000,
        },
        {
            "sequence": 2,
            "task_id": "turn-a",
            "role": "planner",
            "response_model": "SemanticPlanReview",
            "attempt_count": 1,
            "wall_ms": 500,
        },
        {
            "sequence": 3,
            "task_id": "turn-b",
            "role": "planner",
            "response_model": "CoordinatedTurnPlan",
            "attempt_count": 3,
            "wall_ms": 2500,
        },
    ]

    turns = _planner_turns(records)

    assert len(turns) == 2
    assert turns[0].task_id == "turn-a"
    assert turns[0].planner_requests == 2
    assert turns[1].task_id == "turn-b"
    assert turns[1].planner_requests == 3
    assert turns[1].trace == "CoordinatedTurnPlan[x3]"


def test_cascade_warning_threshold_is_high_enough_for_normal_happy_path() -> None:
    assert CASCADE_WARNING_REQUESTS == 10
