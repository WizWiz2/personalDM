from __future__ import annotations

from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.scene_location_table import SceneLocationLink
from app.db.tables import Character, Entity, Scene, SceneParticipant


class PresenceService:
    """Single mutation owner for physical scene participation and character location.

    ``Character.current_location_id`` is authoritative for where a character is now. Historical
    ``SceneParticipant`` rows are intentionally retained because completed scenes and scene bridges
    use them as provenance. SceneStateService remains the authoritative read/invariant checker for
    the active scene.
    """

    def __init__(self, session: AsyncSession):
        self._session = session

    async def add_participant(
        self,
        scene_id: UUID,
        entity_id: UUID,
        *,
        allow_movement: bool = False,
    ) -> bool:
        scene = await self._session.get(Scene, str(scene_id))
        if not scene:
            raise ValueError("Scene not found")

        entity = await self._session.get(Entity, str(entity_id))
        if not entity or entity.campaign_id != scene.campaign_id:
            raise ValueError("Participant must belong to the same campaign as the scene")
        if entity.entity_type != "character":
            raise ValueError("Only character entities may participate in a scene")
        if entity.status in {"dead", "destroyed"}:
            raise ValueError(
                f"Character status {entity.status} cannot participate in a live scene"
            )

        character = await self._session.get(Character, str(entity_id))
        if not character:
            raise ValueError("Character participant has no character-state row")

        scene_location_id = (
            await self._session.execute(
                select(SceneLocationLink.location_id).where(
                    SceneLocationLink.scene_id == str(scene_id)
                )
            )
        ).scalar_one_or_none()

        if scene_location_id:
            target = str(scene_location_id)
            current = character.current_location_id
            if current and current != target and not allow_movement:
                raise ValueError(
                    f"Character is at location {current} and cannot appear at {target} "
                    "without an explicit structured movement"
                )
            if current != target:
                character.current_location_id = target

        existing = (
            await self._session.execute(
                select(SceneParticipant.id).where(
                    SceneParticipant.scene_id == str(scene_id),
                    SceneParticipant.entity_id == str(entity_id),
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            self._session.add(
                SceneParticipant(scene_id=str(scene_id), entity_id=str(entity_id))
            )
        await self._session.flush()
        return True

    async def move_to_location(
        self,
        campaign_id: UUID,
        entity_id: UUID,
        destination_location_id: UUID,
        *,
        expected_origin: UUID,
    ) -> bool:
        """A typed NPC world-event may change position without opening a PC scene.

        The caller MUST approve route and actor goal before reaching this writer.
        The origin comparison is the final defense against stale concurrent turns.
        SceneParticipant on completed scenes remains historical evidence.
        """
        entity = await self._session.get(Entity, str(entity_id))
        destination = await self._session.get(Entity, str(destination_location_id))
        actor = await self._session.get(Character, str(entity_id))
        if (
            not entity or not destination or not actor
            or entity.campaign_id != str(campaign_id)
            or destination.campaign_id != str(campaign_id)
            or entity.entity_type != "character" or destination.entity_type != "location"
            or entity.status in {"dead", "destroyed"}
        ):
            raise ValueError("Structured NPC move references invalid world entities")
        if actor.current_location_id != str(expected_origin):
            raise ValueError("Structured NPC movement origin is stale")
        if destination_location_id == expected_origin:
            raise ValueError("Structured NPC movement must change physical location")
        actor.current_location_id = str(destination_location_id)
        await self._session.flush()
        return True

    async def move_to_scene(self, scene_id: UUID, entity_id: UUID) -> bool:
        return await self.add_participant(
            scene_id,
            entity_id,
            allow_movement=True,
        )

    async def remove_participant(self, scene_id: UUID, entity_id: UUID) -> bool:
        result = await self._session.execute(
            delete(SceneParticipant).where(
                SceneParticipant.scene_id == str(scene_id),
                SceneParticipant.entity_id == str(entity_id),
            )
        )
        await self._session.flush()
        return result.rowcount > 0


__all__ = ["PresenceService"]
