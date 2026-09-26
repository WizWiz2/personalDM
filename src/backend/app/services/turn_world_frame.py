from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.services.scene_state_service import SceneStateService


class TurnWorldFrame(BaseModel):
    """Physical IDs frozen after execution and checked again at publication."""

    scene_id: UUID
    location_id: UUID | None
    participant_ids: list[UUID] = Field(default_factory=list)
    player_location_id: UUID | None

    @classmethod
    async def capture(cls, session: AsyncSession, campaign_id: UUID, scene_id: UUID):
        campaign = await CampaignRepository(session).get_by_id(campaign_id)
        if not campaign or campaign.current_scene_id != scene_id:
            raise ValueError("Published turn scene differs from the campaign's current scene")
        state = await SceneStateService(session).get(campaign_id, scene_id)
        if state.location_id is not None and state.invariant_errors:
            raise ValueError("Invalid prepared world frame: " + "; ".join(state.invariant_errors))
        player = (
            await EntityRepository(session).get_character(campaign.player_character_id)
            if campaign.player_character_id else None
        )
        player_location = player.current_location_id if player else None
        if player and player_location != state.location_id:
            raise ValueError("Player location differs from the published scene's physical location")
        return cls(
            scene_id=scene_id,
            location_id=state.location_id,
            participant_ids=sorted(state.participant_ids, key=str),
            player_location_id=player_location,
        )

    async def assert_unchanged(self, session: AsyncSession, campaign_id: UUID) -> None:
        current = await self.capture(session, campaign_id, self.scene_id)
        if current != self:
            raise ValueError("Prepared physical world changed before turn publication")
