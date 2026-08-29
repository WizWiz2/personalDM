from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.scene_repo import SceneRepository
from app.db.scene_location_table import SceneLocationLink
from app.db.scene_state_table import SceneRuntimeState
from app.db.tables import Campaign, Scene, SceneParticipant
from app.models.scene import SceneRead
from app.services.presence_service import PresenceService


@dataclass(frozen=True)
class SceneActivationResult:
    scene: SceneRead
    previous_scene_id: UUID | None
    changed: bool


class SceneLifecycleService:
    """Own the authoritative active-scene pointer for a campaign.

    Activation is atomic inside the caller's transaction: the target scene becomes active,
    every other active scene is completed and the campaign pointer is updated. Physical
    participation/location mutations are delegated to PresenceService so Scene lifecycle does not
    become a second writer for Character.current_location_id or SceneParticipant.

    SceneParticipant rows on completed scenes are historical provenance and are intentionally kept.
    ``Character.current_location_id`` plus the active scene identify where a character is now.
    """

    def __init__(self, session: AsyncSession):
        self._session = session
        self._scene_repo = SceneRepository(session)
        self._presence = PresenceService(session)

    async def activate(
        self,
        campaign_id: UUID,
        scene_id: UUID,
    ) -> SceneActivationResult:
        campaign = await self._session.get(Campaign, str(campaign_id))
        if not campaign:
            raise ValueError("Campaign not found")

        target = await self._session.get(Scene, str(scene_id))
        if not target or target.campaign_id != str(campaign_id):
            raise ValueError("Scene not found in campaign")
        if target.status == "abandoned":
            raise ValueError("Abandoned scene cannot be activated")

        previous_scene_id = (
            UUID(campaign.current_scene_id) if campaign.current_scene_id else None
        )
        changed = campaign.current_scene_id != target.id or target.status != "active"

        active_scenes = (
            await self._session.execute(
                select(Scene).where(
                    Scene.campaign_id == str(campaign_id),
                    Scene.status == "active",
                )
            )
        ).scalars().all()
        for scene in active_scenes:
            if scene.id != target.id:
                scene.status = "completed"

        target.status = "active"
        campaign.current_scene_id = target.id

        # Validate every already-declared target participant through the single physical writer.
        # NPCs may not be teleported by scene activation. The human player's movement is allowed
        # because activating a structured target scene is itself the authoritative movement edge.
        participant_ids = list(
            (
                await self._session.execute(
                    select(SceneParticipant.entity_id).where(
                        SceneParticipant.scene_id == target.id
                    )
                )
            ).scalars().all()
        )
        for participant_id in participant_ids:
            await self._presence.add_participant(
                scene_id,
                UUID(participant_id),
                allow_movement=(participant_id == campaign.player_character_id),
            )

        if campaign.player_character_id and campaign.player_character_id not in participant_ids:
            await self._presence.move_to_scene(
                scene_id,
                UUID(campaign.player_character_id),
            )

        # Active scenes require a structured location. PresenceService owns location agreement;
        # reading the link here keeps the old fail-fast activation contract explicit.
        location_id = (
            await self._session.execute(
                select(SceneLocationLink.location_id).where(
                    SceneLocationLink.scene_id == target.id
                )
            )
        ).scalar_one_or_none()
        if location_id is None:
            raise ValueError("Cannot activate scene without a structured location")

        runtime = await self._session.get(SceneRuntimeState, target.id)
        if runtime is None:
            self._session.add(
                SceneRuntimeState(scene_id=target.id, world_time_order=0)
            )

        await self._session.flush()

        scene = await self._scene_repo.get_by_id(scene_id)
        if not scene:
            raise ValueError("Activated scene disappeared")
        return SceneActivationResult(
            scene=scene,
            previous_scene_id=previous_scene_id,
            changed=changed,
        )
