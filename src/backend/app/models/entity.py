from datetime import datetime
from enum import Enum
from uuid import UUID
from pydantic import BaseModel, ConfigDict

class EntityType(str, Enum):
    CHARACTER = "character"
    LOCATION = "location"
    FACTION = "faction"
    ITEM = "item"
    CREATURE = "creature"
    ORGANISATION = "organisation"
    CONCEPT = "concept"

class EntityStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    DEAD = "dead"
    DESTROYED = "destroyed"
    UNKNOWN = "unknown"

class EntityCreate(BaseModel):
    entity_type: EntityType
    canonical_name: str
    aliases: list[str] = []
    description: str | None = None
    status: EntityStatus = EntityStatus.ACTIVE
    custom_fields: dict | None = None

class EntityRead(BaseModel):
    id: UUID
    campaign_id: UUID
    entity_type: str
    canonical_name: str
    aliases: list[str] = []
    description: str | None
    status: str
    provenance: str
    version: int
    custom_fields: dict | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

class EntityUpdate(BaseModel):
    canonical_name: str | None = None
    aliases: list[str] | None = None
    description: str | None = None
    status: EntityStatus | None = None
    custom_fields: dict | None = None
