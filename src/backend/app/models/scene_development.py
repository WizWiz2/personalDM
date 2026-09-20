from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class NpcSceneAction(BaseModel):
    """An NPC-owned local act, independent of the human's frozen action sequence.

    This receipt records behavior, not the truth of a character's claims. Spatial, inventory
    and other mechanical mutations still require their respective executors.
    """

    model_config = ConfigDict(extra="forbid")

    actor_id: UUID
    source_refs: list[str] = Field(min_length=1, max_length=4)
    purpose: str = Field(min_length=1, max_length=400)
    action: str = Field(min_length=1, max_length=800)
    player_opportunity: str | None = Field(default=None, max_length=400)


class SceneDevelopment(BaseModel):
    """An explicit pacing decision; quiet is a decision, never a missing model field."""

    model_config = ConfigDict(extra="forbid")

    disposition: Literal["act", "quiet"]
    reason: str = Field(min_length=1, max_length=500)
    actions: list[NpcSceneAction] = Field(max_length=2)

    @model_validator(mode="after")
    def validate_disposition(self):
        if (self.disposition == "act") != bool(self.actions):
            raise ValueError("act requires NPC actions; quiet requires an empty actions list")
        return self
