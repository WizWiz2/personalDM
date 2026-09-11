from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class IdentityBindingDecision(BaseModel):
    """Bounded evidence record for binding a planned NPC designation to an encounter.

    This type is intentionally data-only. Identity authority lives in the planner/materializer guards;
    the model may describe which already-planned introduction/evidence it is referring to, but this
    object cannot create an entity, rename one, or mutate canon by itself.
    """

    model_config = ConfigDict(extra="ignore")

    designation_kind: str = Field(min_length=1, max_length=64)
    binding_source: str = Field(min_length=1, max_length=64)
    introduction_index: int = Field(ge=0, le=8)
    participation: str = Field(min_length=1, max_length=64)
    designation: str = Field(min_length=1, max_length=500)
    encounter_source: str = Field(min_length=1, max_length=64)
    encounter_evidence: str = Field(min_length=1, max_length=1200)
    designation_source: str = Field(min_length=1, max_length=64)
    designation_evidence: str = Field(min_length=1, max_length=1200)
    reason: str = Field(default="", max_length=1000)


__all__ = ["IdentityBindingDecision"]
