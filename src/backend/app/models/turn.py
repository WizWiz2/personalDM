from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, computed_field


TurnRole = Literal[
    "user",
    "assistant",
    "system",
    "meta_user",
    "meta_assistant",
]


class ChatMessage(BaseModel):
    role: str
    content: str


class TurnCreate(BaseModel):
    role: TurnRole
    content: str
    scene_id: UUID | None = None
    # `acting_character_id` is the character who is structurally authorized to own the
    # assistant turn. UI `/talk` selection is different: it is only an addressed NPC and
    # must not disable planning of the player's own actions.
    acting_character_id: UUID | None = None
    addressed_character_id: UUID | None = None
    parent_turn_id: UUID | None = None
    model_name: str | None = None
    context_snapshot: dict | None = None
    token_count: int | None = None


class TurnRead(BaseModel):
    id: UUID
    campaign_id: UUID
    scene_id: UUID | None
    acting_character_id: UUID | None
    role: str
    content: str
    parent_turn_id: UUID | None
    status: str
    model_name: str | None
    token_count: int | None
    created_at: datetime

    # Internal services (notably PostTurnProcessor) need the persisted authority snapshot to
    # distinguish TurnAuthority-managed turns from legacy prose-only turns. Keep it off public
    # serialization while still preserving it when TurnRepository materializes a DB row.
    context_snapshot: str | dict | None = Field(default=None, exclude=True)

    @computed_field
    @property
    def channel(self) -> Literal["narrative", "meta"]:
        return "meta" if self.role.startswith("meta_") else "narrative"

    model_config = ConfigDict(from_attributes=True)
