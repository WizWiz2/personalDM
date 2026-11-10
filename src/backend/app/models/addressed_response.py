from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


RequestDecision = Literal["accepted", "refused", "conditional", "deferred"]


class RequestOutcome(BaseModel):
    """A decision about a request, independent of prose and execution."""

    decision: RequestDecision
    continuation_of: UUID | None = None
    unchanged: bool = False
    standalone: bool = False


class AddressedResponse(BaseModel):
    """Who the player addressed, and a typed name change. Never pre-written speech."""

    model_config = ConfigDict(extra="ignore")

    speaker_name: str | None = Field(default=None, max_length=120)
    speaker_id: UUID | None = None
    after_action_index: int | None = Field(default=None, ge=0, le=7)
    revealed_name: str | None = Field(default=None, min_length=2, max_length=120)
    name_evidence: str | None = Field(default=None, max_length=500)
    request_outcome: RequestOutcome | None = None

    @model_validator(mode="after")
    def validate_name_revelation(self):
        if self.revealed_name and (
            not self.name_evidence or self.revealed_name not in self.name_evidence
        ):
            raise ValueError("name revelation requires exact self-identification evidence")
        return self
