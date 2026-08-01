import json
from uuid import UUID

from sqlalchemy import delete, select

from app.db.repositories.base import BaseRepository
from app.db.scene_location_table import SceneLocationLink
from app.db.tables import Entity, Scene, SceneParticipant, SceneThesis
from app.models.scene import SceneCreate, SceneRead, SceneUpdate
from app.models.scene_thesis import (
    SceneThesisCreate,
    SceneThesisRead,
    SceneThesisUpdate,
)


class SceneRepository(BaseRepository):
    async def create(self, campaign_id: UUID, data: SceneCreate) -> SceneRead:
        db_scene = Scene(
            campaign_id=str(campaign_id),
            title=data.title,
            location_description=data.location_description,
            mood=data.mood,
            tension=data.tension,
            status="active",
        )
        self._session.add(db_scene)
        await self._session.flush()
        if data.location_id:
            await self._set_location(
                UUID(db_scene.id),
                campaign_id,
                data.location_id,
            )
        return await self._to_scene_read(db_scene)

    async def get_by_id(self, scene_id: UUID) -> SceneRead | None:
        result = await self._session.execute(
            select(Scene).where(Scene.id == str(scene_id))
        )
        db_scene = result.scalar_one_or_none()
        if not db_scene:
            return None
        return await self._to_scene_read(db_scene)

    async def list_by_campaign(self, campaign_id: UUID) -> list[SceneRead]:
        result = await self._session.execute(
            select(Scene)
            .where(Scene.campaign_id == str(campaign_id))
            .order_by(Scene.created_at.desc())
        )
        return [
            await self._to_scene_read(db_scene)
            for db_scene in result.scalars().all()
        ]

    async def update(
        self,
        scene_id: UUID,
        data: SceneUpdate,
    ) -> SceneRead | None:
        result = await self._session.execute(
            select(Scene).where(Scene.id == str(scene_id))
        )
        db_scene = result.scalar_one_or_none()
        if not db_scene:
            return None

        values = data.model_dump(exclude_unset=True)
        location_was_set = "location_id" in values
        location_id = values.pop("location_id", None)
        for key, value in values.items():
            setattr(db_scene, key, value)
        if location_was_set:
            await self._set_location(
                scene_id,
                UUID(db_scene.campaign_id),
                location_id,
            )
        await self._session.flush()
        return await self._to_scene_read(db_scene)

    async def delete(self, scene_id: UUID) -> bool:
        result = await self._session.execute(
            select(Scene).where(Scene.id == str(scene_id))
        )
        db_scene = result.scalar_one_or_none()
        if not db_scene:
            return False
        await self._session.delete(db_scene)
        await self._session.flush()
        return True

    async def get_location_id(self, scene_id: UUID) -> UUID | None:
        result = await self._session.execute(
            select(SceneLocationLink.location_id).where(
                SceneLocationLink.scene_id == str(scene_id)
            )
        )
        value = result.scalar_one_or_none()
        return UUID(value) if value else None

    async def add_participant(self, scene_id: UUID, entity_id: UUID) -> bool:
        scene_result = await self._session.execute(
            select(Scene).where(Scene.id == str(scene_id))
        )
        scene = scene_result.scalar_one_or_none()
        if not scene:
            raise ValueError("Scene not found")

        entity_result = await self._session.execute(
            select(Entity).where(Entity.id == str(entity_id))
        )
        entity = entity_result.scalar_one_or_none()
        if not entity or entity.campaign_id != scene.campaign_id:
            raise ValueError("Participant must belong to the same campaign as the scene")
        if entity.entity_type != "character":
            raise ValueError("Only character entities may participate in a scene")

        result = await self._session.execute(
            select(SceneParticipant).where(
                SceneParticipant.scene_id == str(scene_id),
                SceneParticipant.entity_id == str(entity_id),
            )
        )
        if result.scalar_one_or_none():
            return True

        self._session.add(
            SceneParticipant(scene_id=str(scene_id), entity_id=str(entity_id))
        )
        await self._session.flush()
        return True

    async def remove_participant(self, scene_id: UUID, entity_id: UUID) -> bool:
        result = await self._session.execute(
            delete(SceneParticipant).where(
                SceneParticipant.scene_id == str(scene_id),
                SceneParticipant.entity_id == str(entity_id),
            )
        )
        await self._session.flush()
        return result.rowcount > 0

    async def get_participants(self, scene_id: UUID) -> list[UUID]:
        result = await self._session.execute(
            select(SceneParticipant.entity_id).where(
                SceneParticipant.scene_id == str(scene_id)
            )
        )
        return [UUID(value) for value in result.scalars().all()]

    async def create_thesis(
        self,
        scene_id: UUID,
        data: SceneThesisCreate,
        source_turn_id: UUID | None = None,
    ) -> SceneThesisRead:
        db_thesis = SceneThesis(
            scene_id=str(scene_id),
            thesis_type=data.thesis_type.value,
            text=data.text,
            priority=data.priority,
            status="active",
            visibility=data.visibility,
            source_turn_id=str(source_turn_id) if source_turn_id else None,
            pinned=data.pinned,
            related_entity_ids=json.dumps(
                [str(entity_id) for entity_id in data.related_entity_ids]
            ),
        )
        self._session.add(db_thesis)
        await self._session.flush()
        return self._to_thesis_read(db_thesis)

    async def get_thesis_by_id(
        self,
        thesis_id: UUID,
    ) -> SceneThesisRead | None:
        result = await self._session.execute(
            select(SceneThesis).where(SceneThesis.id == str(thesis_id))
        )
        db_thesis = result.scalar_one_or_none()
        if not db_thesis:
            return None
        return self._to_thesis_read(db_thesis)

    async def list_theses_by_scene(
        self,
        scene_id: UUID,
        active_only: bool = True,
    ) -> list[SceneThesisRead]:
        query = select(SceneThesis).where(SceneThesis.scene_id == str(scene_id))
        if active_only:
            query = query.where(SceneThesis.status == "active")
        result = await self._session.execute(query)
        return [self._to_thesis_read(item) for item in result.scalars().all()]

    async def update_thesis(
        self,
        thesis_id: UUID,
        data: SceneThesisUpdate,
    ) -> SceneThesisRead | None:
        result = await self._session.execute(
            select(SceneThesis).where(SceneThesis.id == str(thesis_id))
        )
        db_thesis = result.scalar_one_or_none()
        if not db_thesis:
            return None

        for key, value in data.model_dump(exclude_unset=True).items():
            if key == "related_entity_ids" and value is not None:
                setattr(db_thesis, key, json.dumps([str(item) for item in value]))
            else:
                setattr(db_thesis, key, value)
        await self._session.flush()
        return self._to_thesis_read(db_thesis)

    async def delete_thesis(self, thesis_id: UUID) -> bool:
        result = await self._session.execute(
            select(SceneThesis).where(SceneThesis.id == str(thesis_id))
        )
        db_thesis = result.scalar_one_or_none()
        if not db_thesis:
            return False
        await self._session.delete(db_thesis)
        await self._session.flush()
        return True

    async def pin_thesis(self, thesis_id: UUID) -> bool:
        result = await self._session.execute(
            select(SceneThesis).where(SceneThesis.id == str(thesis_id))
        )
        db_thesis = result.scalar_one_or_none()
        if not db_thesis:
            return False
        db_thesis.pinned = True
        await self._session.flush()
        return True

    async def resolve_thesis(self, thesis_id: UUID) -> bool:
        result = await self._session.execute(
            select(SceneThesis).where(SceneThesis.id == str(thesis_id))
        )
        db_thesis = result.scalar_one_or_none()
        if not db_thesis:
            return False
        db_thesis.status = "resolved"
        await self._session.flush()
        return True

    async def _set_location(
        self,
        scene_id: UUID,
        campaign_id: UUID,
        location_id: UUID | None,
    ) -> None:
        await self._session.execute(
            delete(SceneLocationLink).where(
                SceneLocationLink.scene_id == str(scene_id)
            )
        )
        if location_id is None:
            return
        location = await self._session.get(Entity, str(location_id))
        if (
            not location
            or location.campaign_id != str(campaign_id)
            or location.entity_type != "location"
        ):
            raise ValueError("Scene location must be a location in the same campaign")
        self._session.add(
            SceneLocationLink(
                scene_id=str(scene_id),
                location_id=str(location_id),
            )
        )
        await self._session.flush()

    async def _to_scene_read(self, db_scene: Scene) -> SceneRead:
        result = await self._session.execute(
            select(SceneLocationLink.location_id, Entity)
            .join(Entity, Entity.id == SceneLocationLink.location_id)
            .where(SceneLocationLink.scene_id == db_scene.id)
        )
        location_row = result.first()
        location_id = UUID(location_row[0]) if location_row else None
        location_entity = location_row[1] if location_row else None
        location_description = db_scene.location_description
        if location_entity and not location_description:
            location_description = location_entity.canonical_name
            if location_entity.description:
                location_description += f" — {location_entity.description}"

        return SceneRead(
            id=UUID(db_scene.id),
            campaign_id=UUID(db_scene.campaign_id),
            title=db_scene.title,
            location_id=location_id,
            location_description=location_description,
            mood=db_scene.mood,
            tension=db_scene.tension,
            status=db_scene.status,
            participants=await self.get_participants(UUID(db_scene.id)),
            created_at=db_scene.created_at,
            updated_at=db_scene.updated_at,
        )

    @staticmethod
    def _to_thesis_read(db_thesis: SceneThesis) -> SceneThesisRead:
        related_ids = []
        if db_thesis.related_entity_ids:
            try:
                related_ids = [
                    UUID(value)
                    for value in json.loads(db_thesis.related_entity_ids)
                ]
            except Exception:
                related_ids = []

        return SceneThesisRead(
            id=UUID(db_thesis.id),
            scene_id=UUID(db_thesis.scene_id),
            thesis_type=db_thesis.thesis_type,
            text=db_thesis.text,
            priority=db_thesis.priority,
            status=db_thesis.status,
            visibility=db_thesis.visibility,
            source_turn_id=(
                UUID(db_thesis.source_turn_id)
                if db_thesis.source_turn_id
                else None
            ),
            pinned=db_thesis.pinned,
            related_entity_ids=related_ids,
            created_at=db_thesis.created_at,
            updated_at=db_thesis.updated_at,
        )
