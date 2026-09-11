from live_model_contracts.diagnostic_console_runner import (
    CASCADE_WARNING_REQUESTS,
    _attempt_count,
    _collapsed_trace,
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


def test_cascade_warning_threshold_is_high_enough_for_normal_happy_path() -> None:
    assert CASCADE_WARNING_REQUESTS == 10
