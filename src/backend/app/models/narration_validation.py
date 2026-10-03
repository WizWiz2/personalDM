from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

_TRACE_TEXT_LIMIT = 300
_EXCERPT_CONTEXT = 80


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

    def trace(self, candidate: str) -> dict:
        """Compact record of this violation and the exact candidate span it rejected."""
        evidence = self.evidence.strip()
        start = candidate.find(evidence)
        span = None
        if start >= 0:
            end = start + len(evidence)
            span = {
                "start": start,
                "end": end,
                "excerpt": candidate[max(0, start - _EXCERPT_CONTEXT) : end + _EXCERPT_CONTEXT],
            }
        return {
            "code": self.violation_type,
            "severity": self.severity,
            "evidence": evidence[:_TRACE_TEXT_LIMIT],
            "correction": self.correction[:_TRACE_TEXT_LIMIT],
            "span": span,
        }


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

    def trace(self, candidate: str) -> dict:
        """Compact decision payload: the verdict, every violation and a short candidate head."""
        return {
            "verdict": self.verdict,
            "summary": self.summary[:_TRACE_TEXT_LIMIT],
            "candidate_characters": len(candidate),
            "candidate_excerpt": candidate[:_TRACE_TEXT_LIMIT],
            "violations": [item.trace(candidate) for item in self.violations],
        }

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
