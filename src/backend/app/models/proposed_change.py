from datetime import datetime
from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ChangeType(str, Enum):
    FACT = "fact"
    EVENT = "event"
    RELATIONSHIP = "relationship"
    SCENE_THESIS = "scene_thesis"
    MOVEMENT = "movement"
    KNOWLEDGE = "knowledge"
    ITEM_TRANSFER = "item_transfer"
    CANON_GAP = "canon_gap"


class ProposedChangeCreate(BaseModel):
    change_type: ChangeType
    payload: dict


class ProposedChangeRead(BaseModel):
    id: UUID
    turn_id: UUID
    change_type: str
    payload: dict
    status: str
    user_edit: dict | None = None
    created_at: datetime
    resolved_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class ProposalAction(BaseModel):
    status: Literal["accepted", "rejected", "edited"]
    user_edit: dict | None = None
