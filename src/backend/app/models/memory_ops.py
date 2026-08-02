from uuid import UUID

from pydantic import BaseModel, Field


class MemoryMaintenanceRequest(BaseModel):
    apply_changes: bool = False
    clean_expired_details: bool = True
    close_stale_theses: bool = True
    repair_missing_profiles: bool = True


class MemoryMaintenanceAction(BaseModel):
    action: str
    target_type: str
    target_id: UUID
    reason: str
    changes_data: bool = False


class MemoryMaintenanceResult(BaseModel):
    applied: bool
    campaign_id: UUID
    actions: list[MemoryMaintenanceAction] = Field(default_factory=list)
    details_cleaned: int = 0
    theses_closed: int = 0
    profiles_repaired: int = 0
