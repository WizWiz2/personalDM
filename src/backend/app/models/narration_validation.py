from __future__ import annotations

from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class NarrationViolation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    violation_type: Literal[
        "absent_character",
        "absent_object",
        "invalid_movement",
        "invalid_time_advance",
        "player_agency",
        "ungrounded_complication",
        "sequence_violation",
        "canon_conflict",
        "speaker_consistency",
        "meta_language",
        "other",
    ]
    severity: Literal["warning", "error"] = "error"
    evidence: str = Field(min_length=1, max_length=1000)
    correction: str = Field(min_length=1, max_length=1000)

    @field_validator("violation_type", mode="before")
    @classmethod
    def normalize_reported_category(cls, value):
        # The finding's severity/evidence/correction still bind. An unfamiliar label
        # is an uncategorized finding, not a reason to rerun the whole review.
        if isinstance(value, str) and value.strip():
            normalized = value.strip()
            known = get_args(cls.model_fields["violation_type"].annotation)
            return normalized if normalized in known else "other"
        return value


class NarrationQuestionCoverage(BaseModel):
    """Reviewer-selected exact prose evidence for one frozen information request."""

    question_index: int = Field(ge=0, le=7)
    evidence: str = Field(min_length=1, max_length=500)


class NarrationValidationResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    verdict: Literal["pass", "repair_required"]
    summary: str = Field(default="", max_length=1500)
    violations: list[NarrationViolation] = Field(default_factory=list, max_length=12)
    response_coverage: list[NarrationQuestionCoverage] = Field(default_factory=list, max_length=8)

    def covers_questions(self, question_count: int, candidate: str) -> bool:
        indices = [item.question_index for item in self.response_coverage]
        return (
            len(indices) == len(set(indices))
            and set(indices) == set(range(question_count))
            and all(
                item.evidence.strip() and item.evidence.strip() in candidate
                for item in self.response_coverage
            )
        )

    @model_validator(mode="after")
    def validate_verdict(self):
        errors = [item for item in self.violations if item.severity == "error"]
        if self.verdict == "pass" and errors:
            raise ValueError("pass verdict cannot contain error violations")
        if self.verdict == "repair_required" and not errors:
            raise ValueError("repair_required needs at least one error violation")
        return self
