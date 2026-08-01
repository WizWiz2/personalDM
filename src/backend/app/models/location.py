from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.entity import EntityStatus


class LocationCreate(BaseModel):
    canonical_name: str
    aliases: list[str] = Field(default_factory=list)
    description: str | None = None
    status: EntityStatus = EntityStatus.ACTIVE
    geography: str | None = None
    atmosphere: str | None = None
    access_rules: str | None = None
    parent_location_id: UUID | None = None
    climate: str | None = None
    notable_features: str | None = None
    danger_level: str | None = None
    custom_fields: dict | None = None


class LocationRead(BaseModel):
    id: UUID
    campaign_id: UUID
    entity_type: str
    canonical_name: str
    aliases: list[str] = Field(default_factory=list)
    description: str | None
    status: str
    provenance: str
    version: int
    custom_fields: dict | None
    geography: str | None
    atmosphere: str | None
    access_rules: str | None
    parent_location_id: UUID | None
    climate: str | None
    notable_features: str | None
    danger_level: str | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class LocationUpdate(BaseModel):
    canonical_name: str | None = None
    aliases: list[str] | None = None
    description: str | None = None
    status: EntityStatus | None = None
    geography: str | None = None
    atmosphere: str | None = None
    access_rules: str | None = None
    parent_location_id: UUID | None = None
    climate: str | None = None
    notable_features: str | None = None
    danger_level: str | None = None
    custom_fields: dict | None = None
