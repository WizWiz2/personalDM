from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
        "other",
    ]
    severity: Literal["warning", "error"] = "error"
    evidence: str = Field(min_length=1, max_length=1000)
    correction: str = Field(min_length=1, max_length=1000)


class NarrationValidationResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    verdict: Literal["pass", "repair_required"]
    summary: str = Field(default="", max_length=1500)
    violations: list[NarrationViolation] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def validate_verdict(self):
        errors = [item for item in self.violations if item.severity == "error"]
        if self.verdict == "pass" and errors:
            raise ValueError("pass verdict cannot contain error violations")
        if self.verdict == "repair_required" and not errors:
            raise ValueError("repair_required needs at least one error violation")
        return self
