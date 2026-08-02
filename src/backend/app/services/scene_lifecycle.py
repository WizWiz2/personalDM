from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.narrative_detail_repo import NarrativeDetailRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.scene_location_table import SceneLocationLink
from app.db.scene_state_table import SceneRuntimeState
from app.db.tables import Campaign, Character, Scene, SceneParticipant
from app.models.scene import SceneRead


@dataclass(frozen=True)
class SceneActivationResult:
    scene: SceneRead
    previous_scene_id: UUID | None
    changed: bool


class SceneLifecycleService:
    """Own the authoritative active-scene pointer for a campaign.

    Activation is atomic inside the caller's transaction: the target scene becomes
    active, every other active scene is completed, the campaign pointer is updated,
    and every physical participant must agree with the scene location. The player is
    inserted automatically; an NPC already located elsewhere is rejected rather than
    silently teleported. Short-lived narrative details expire with their scene.
    """

    def __init__(self, session: AsyncSession):
        self._session = session
        self._scene_repo = SceneRepository(session)
        self._narrative_details = NarrativeDetailRepository(session)

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
                await self._narrative_details.expire_scene(UUID(scene.id))

        target.status = "active"
        campaign.current_scene_id = target.id

        location_result = await self._session.execute(
            select(SceneLocationLink.location_id).where(
                SceneLocationLink.scene_id == target.id
            )
        )
        location_id = location_result.scalar_one_or_none()

        participant_ids = set(
            (
                await self._session.execute(
                    select(SceneParticipant.entity_id).where(
                        SceneParticipant.scene_id == target.id
                    )
                )
            ).scalars().all()
        )
        if (
            campaign.player_character_id
            and campaign.player_character_id not in participant_ids
        ):
            self._session.add(
                SceneParticipant(
                    scene_id=target.id,
                    entity_id=campaign.player_character_id,
                )
            )
            participant_ids.add(campaign.player_character_id)

        if location_id:
            for participant_id in participant_ids:
                character = await self._session.get(Character, participant_id)
                if not character:
                    raise ValueError(
                        f"Scene participant {participant_id} has no character state"
                    )
                if participant_id == campaign.player_character_id:
                    character.current_location_id = location_id
                    continue
                if character.current_location_id is None:
                    character.current_location_id = location_id
                elif character.current_location_id != location_id:
                    raise ValueError(
                        "Cannot activate scene: participant is physically located "
                        f"elsewhere ({participant_id}: {character.current_location_id})"
                    )

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
