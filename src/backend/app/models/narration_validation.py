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


class GrantedBeat(BaseModel):
    """The narrator's typed claim of how the beat owner took the beat, with exact prose evidence."""

    model_config = ConfigDict(extra="forbid")

    cast_id: str = Field(min_length=1, max_length=64)
    kind: Literal["speech", "refusal", "leave", "act"]
    evidence: str = Field(min_length=1)

    def failure(self, owner_id: object, owner_name: str, prose: str) -> str | None:
        """Structural check: owner ID, exact fragment; speech overlaps a dialogue line or quote span,
        an act or leave has the owner as grammatical subject (a refusal may be either)."""
        if self.cast_id != str(owner_id):
            return f"beat cast_id {self.cast_id} is not the grant owner {owner_id}"
        evidence = self.evidence.strip()
        start = prose.find(evidence)
        if not evidence or start < 0:
            return "beat evidence is not an exact fragment of the prose"
        lines = prose[prose.rfind("\n", 0, start) + 1:start + len(evidence)].split("\n")
        before = prose[:start]
        spoken = (
            any(line.lstrip().startswith(("—", "–")) for line in lines)
            or any(mark in evidence for mark in "«„\"")
            or before.count("«") > before.count("»")
            or before.count("„") > before.count("“")
            or before.count('"') % 2 == 1
        )
        if self.kind == "speech" or (spoken and self.kind == "refusal"):
            return None if spoken else "speech evidence is outside any dialogue line or quote"
        from app.services.linguistic_intent_analyzer import LinguisticIntentAnalyzer

        if not LinguisticIntentAnalyzer().subject_is(prose, evidence, owner_name):
            return f"{self.kind} evidence does not have {owner_name} as its grammatical subject"
        return None


class GrantedNarration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prose: str = Field(min_length=1)
    beat: GrantedBeat


class NarrationValidationResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    verdict: Literal["pass", "repair_required"]
    summary: str = Field(default="", max_length=1500)
    violations: list[NarrationViolation] = Field(default_factory=list, max_length=12)

    def trace(self, candidate: str) -> dict:
        """Compact decision payload: the verdict, every violation and a short candidate head."""
        return {
            "verdict": self.verdict,
            "summary": self.summary[:_TRACE_TEXT_LIMIT],
            "candidate_characters": len(candidate),
            "candidate_excerpt": candidate[:_TRACE_TEXT_LIMIT],
            "violations": [item.trace(candidate) for item in self.violations],
        }

    @model_validator(mode="after")
    def validate_verdict(self):
        errors = [item for item in self.violations if item.severity == "error"]
        if self.verdict == "pass" and errors:
            raise ValueError("pass verdict cannot contain error violations")
        if self.verdict == "repair_required" and not errors:
            raise ValueError("repair_required needs at least one error violation")
        return self
