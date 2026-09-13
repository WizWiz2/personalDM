from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class PlanReviewAssessment(BaseModel):
    """One independently checked reviewer objection.

    The adjudicator may classify an objection, but it cannot rewrite the candidate plan. A verdict
    is engine-certifiable only when ``plan_path`` resolves inside the exact candidate and
    ``observed_value`` equals the value at that path.
    """

    model_config = ConfigDict(extra="ignore")

    issue_index: int = Field(ge=0, le=31)
    verdict: Literal["supported", "unsupported", "uncertain"]
    plan_path: str | None = Field(default=None, max_length=240)
    observed_value: Any = None
    reason: str = Field(default="", max_length=1000)


class PlanReviewAdjudication(BaseModel):
    """Bounded second opinion over reviewer objections; never an executable-plan rewrite."""

    model_config = ConfigDict(extra="ignore")

    plan_valid: bool = False
    assessments: list[PlanReviewAssessment] = Field(default_factory=list, max_length=32)
    remaining_issues: list[str] = Field(default_factory=list, max_length=16)


PROMPT = """[PLAN REVIEW ADJUDICATION — READ ONLY]
You receive the latest human input, immutable campaign context, one already-typed candidate plan,
reviewer objections, and machine-owned engine authority flags. You are a second-opinion auditor, not
a Planner. Never rewrite, extend, reorder, or repair the plan.

For every objection you can independently evaluate, return one assessment with its numeric
issue_index. verdict is exactly one of: supported, unsupported, uncertain.

A verdict is accepted by the engine only when it is anchored to the candidate itself:
- set plan_path to one concrete path in the supplied candidate plan, using dot-separated mapping
  keys and zero-based list indexes (example: action_sequence.steps.0.transition.required);
- copy the value at that exact path verbatim into observed_value;
- explain briefly why that exact field supports or disproves the objection.
If no candidate field can certify the conclusion, use uncertain and leave plan_path null.

Machine-owned engine_authority flags outrank semantic speculation. Do not infer additional travel,
presence, inventory ownership, or player commitments from prose. Do not treat a merely plausible
alternative outcome as a defect in the typed candidate.

plan_valid may summarize your opinion but is never sufficient by itself to override the reviewer.
Return only valid PlanReviewAdjudication JSON.
"""


def _path_value(candidate: dict[str, Any], path: str | None) -> tuple[bool, Any]:
    if not path:
        return False, None
    current: Any = candidate
    for raw_part in path.split("."):
        part = raw_part.strip()
        if not part:
            return False, None
        if isinstance(current, dict):
            if part not in current:
                return False, None
            current = current[part]
            continue
        if isinstance(current, list):
            try:
                index = int(part)
            except (TypeError, ValueError):
                return False, None
            if index < 0 or index >= len(current):
                return False, None
            current = current[index]
            continue
        return False, None
    return True, current


def _is_certified(item: PlanReviewAssessment, candidate: dict[str, Any]) -> bool:
    resolved, value = _path_value(candidate, item.plan_path)
    return resolved and value == item.observed_value


def certified_assessments(
    assessment: PlanReviewAdjudication | dict[str, Any],
    candidate: dict[str, Any],
    objection_count: int,
) -> PlanReviewAdjudication:
    """Drop duplicate, out-of-range, or unanchored LLM judgements.

    Failing certification is intentionally not an error. The caller keeps the original reviewer
    rejection, which is the safer legacy behavior while production migrates away from this loop.
    """

    parsed = (
        assessment
        if isinstance(assessment, PlanReviewAdjudication)
        else PlanReviewAdjudication.model_validate(assessment)
    )
    seen: set[int] = set()
    certified: list[PlanReviewAssessment] = []
    for item in parsed.assessments:
        if item.issue_index in seen or not 0 <= item.issue_index < objection_count:
            continue
        if not _is_certified(item, candidate):
            continue
        seen.add(item.issue_index)
        certified.append(item)
    return PlanReviewAdjudication(
        plan_valid=parsed.plan_valid,
        assessments=certified,
        remaining_issues=list(parsed.remaining_issues),
    )


def all_objections_disproved(
    assessment: PlanReviewAdjudication | dict[str, Any],
    candidate: dict[str, Any],
    objection_count: int,
) -> bool:
    """Allow reviewer override only when every objection is independently and exactly certified."""

    if objection_count <= 0:
        return False
    certified = certified_assessments(assessment, candidate, objection_count)
    by_index = {item.issue_index: item for item in certified.assessments}
    return (
        len(by_index) == objection_count
        and all(by_index[index].verdict == "unsupported" for index in range(objection_count))
        and not any(issue.strip() for issue in certified.remaining_issues)
    )


__all__ = [
    "PROMPT",
    "PlanReviewAdjudication",
    "PlanReviewAssessment",
    "all_objections_disproved",
    "certified_assessments",
]
