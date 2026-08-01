from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.scene_repo import SceneRepository
from app.db.tables import Campaign, Scene
from app.models.scene import SceneRead


@dataclass(frozen=True)
class SceneActivationResult:
    scene: SceneRead
    previous_scene_id: UUID | None
    changed: bool


class SceneLifecycleService:
    """Own the authoritative active-scene pointer for a campaign.

    Scene prose may mention movement, but only this service changes the structured
    scene state. Activation is atomic inside the caller's transaction: the target
    scene becomes active, every other active scene is completed, and the campaign
    pointer is updated.
    """

    def __init__(self, session: AsyncSession):
        self._session = session
        self._scene_repo = SceneRepository(session)

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
        await self._session.flush()

        scene = await self._scene_repo.get_by_id(scene_id)
        if not scene:  # Defensive: the row was loaded above and must still exist.
            raise ValueError("Activated scene disappeared")
        return SceneActivationResult(
            scene=scene,
            previous_scene_id=previous_scene_id,
            changed=changed,
        )
