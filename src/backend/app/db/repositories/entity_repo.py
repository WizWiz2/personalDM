import json
from uuid import UUID

from sqlalchemy import or_, select

from app.db.repositories.base import BaseRepository
from app.db.tables import Character, Entity, SceneParticipant
from app.models.belief import BeliefRead
from app.models.character import CharacterCreate, CharacterRead, CharacterUpdate
from app.models.entity import EntityCreate, EntityRead, EntityStatus, EntityType, EntityUpdate
from app.models.goal import GoalRead
from app.models.relationship import RelationshipRead


class EntityRepository(BaseRepository):
    async def create(self, campaign_id: UUID, data: EntityCreate) -> EntityRead:
        db_entity = Entity(
            campaign_id=str(campaign_id),
            entity_type=data.entity_type.value,
            canonical_name=data.canonical_name,
            aliases=json.dumps(data.aliases or []),
            description=data.description,
            status=data.status.value,
            provenance="manual",
            version=1,
            custom_fields=(
                json.dumps(data.custom_fields) if data.custom_fields is not None else None
            ),
        )
        self._session.add(db_entity)
        await self._session.flush()
        return self._to_entity_read(db_entity)

    async def get_by_id(self, entity_id: UUID) -> EntityRead | None:
        result = await self._session.execute(
            select(Entity).where(Entity.id == str(entity_id))
        )
        db_entity = result.scalar_one_or_none()
        return self._to_entity_read(db_entity) if db_entity else None

    async def list_by_campaign(
        self,
        campaign_id: UUID,
        entity_type: str | None = None,
    ) -> list[EntityRead]:
        query = select(Entity).where(Entity.campaign_id == str(campaign_id))
        if entity_type:
            query = query.where(Entity.entity_type == entity_type)
        result = await self._session.execute(query)
        return [self._to_entity_read(entity) for entity in result.scalars().all()]

    async def update(self, entity_id: UUID, data: EntityUpdate) -> EntityRead | None:
        result = await self._session.execute(
            select(Entity).where(Entity.id == str(entity_id))
        )
        db_entity = result.scalar_one_or_none()
        if not db_entity:
            return None

        for key, value in data.model_dump(exclude_unset=True).items():
            if key == "aliases" and value is not None:
                db_entity.aliases = json.dumps(value)
            elif key == "custom_fields":
                db_entity.custom_fields = (
                    json.dumps(value) if value is not None else None
                )
            elif key == "status" and value is not None:
                db_entity.status = value.value
            elif key in {"canonical_name"} and value is None:
                continue
            else:
                setattr(db_entity, key, value)

        db_entity.version += 1
        await self._session.flush()
        return self._to_entity_read(db_entity)

    async def delete(self, entity_id: UUID) -> bool:
        result = await self._session.execute(
            select(Entity).where(Entity.id == str(entity_id))
        )
        db_entity = result.scalar_one_or_none()
        if not db_entity:
            return False
        await self._session.delete(db_entity)
        await self._session.flush()
        return True

    async def search_by_name(
        self,
        campaign_id: UUID,
        query: str,
    ) -> list[EntityRead]:
        pattern = f"%{query}%"
        result = await self._session.execute(
            select(Entity).where(
                Entity.campaign_id == str(campaign_id),
                or_(
                    Entity.canonical_name.like(pattern),
                    Entity.aliases.like(pattern),
                ),
            )
        )
        return [self._to_entity_read(entity) for entity in result.scalars().all()]

    async def create_character(
        self,
        campaign_id: UUID,
        data: CharacterCreate,
    ) -> CharacterRead:
        db_entity = Entity(
            campaign_id=str(campaign_id),
            entity_type=EntityType.CHARACTER.value,
            canonical_name=data.canonical_name,
            aliases=json.dumps(data.aliases or []),
            description=data.description,
            status=data.status.value,
            provenance="manual",
            version=1,
            custom_fields=(
                json.dumps(data.custom_fields) if data.custom_fields is not None else None
            ),
        )
        self._session.add(db_entity)
        await self._session.flush()

        db_character = Character(
            entity_id=db_entity.id,
            appearance=data.appearance,
            face_description=data.face_description,
            body_description=data.body_description,
            immutable_features=data.immutable_features,
            personality=data.personality,
            values=json.dumps(data.values or []),
            fears=json.dumps(data.fears or []),
            desires=json.dumps(data.desires or []),
            voice=data.voice,
            speech_patterns=data.speech_patterns,
            biography=data.biography,
            backstory_public=data.backstory_public,
            backstory_secret=data.backstory_secret,
            emotional_state=data.emotional_state,
            current_location_id=(
                str(data.current_location_id) if data.current_location_id else None
            ),
            current_intentions=json.dumps(data.current_intentions or []),
            visual_profile=(
                json.dumps(data.visual_profile)
                if data.visual_profile is not None
                else None
            ),
        )
        self._session.add(db_character)
        await self._session.flush()
        return self._to_character_read(db_entity, db_character)

    async def get_character(self, entity_id: UUID) -> CharacterRead | None:
        result = await self._session.execute(
            select(Entity, Character)
            .join(Character, Entity.id == Character.entity_id)
            .where(Entity.id == str(entity_id))
        )
        row = result.first()
        if not row:
            return None
        return self._to_character_read(row[0], row[1])

    async def update_character(
        self,
        entity_id: UUID,
        data: CharacterUpdate,
    ) -> CharacterRead | None:
        update_dict = data.model_dump(exclude_unset=True)
        base_keys = {
            "canonical_name",
            "aliases",
            "description",
            "status",
            "custom_fields",
        }
        base_payload = {
            key: value
            for key, value in update_dict.items()
            if key in base_keys
        }
        if base_payload:
            if not await self.update(entity_id, EntityUpdate(**base_payload)):
                return None
        elif not await self.get_by_id(entity_id):
            return None

        result = await self._session.execute(
            select(Character).where(Character.entity_id == str(entity_id))
        )
        db_character = result.scalar_one_or_none()
        if not db_character:
            return None

        character_keys = {
            "appearance",
            "face_description",
            "body_description",
            "immutable_features",
            "personality",
            "values",
            "fears",
            "desires",
            "voice",
            "speech_patterns",
            "biography",
            "backstory_public",
            "backstory_secret",
            "emotional_state",
            "current_location_id",
            "current_intentions",
            "visual_profile",
        }
        for key, value in update_dict.items():
            if key not in character_keys:
                continue
            if key in {"values", "fears", "desires", "current_intentions"}:
                setattr(
                    db_character,
                    key,
                    json.dumps(value) if value is not None else json.dumps([]),
                )
            elif key == "visual_profile":
                db_character.visual_profile = (
                    json.dumps(value) if value is not None else None
                )
            elif key == "current_location_id":
                db_character.current_location_id = str(value) if value else None
            else:
                setattr(db_character, key, value)

        await self._session.flush()
        return await self.get_character(entity_id)

    async def get_characters_in_scene(
        self,
        scene_id: UUID,
    ) -> list[CharacterRead]:
        result = await self._session.execute(
            select(Entity, Character)
            .join(Character, Entity.id == Character.entity_id)
            .join(SceneParticipant, Entity.id == SceneParticipant.entity_id)
            .where(SceneParticipant.scene_id == str(scene_id))
        )
        return [self._to_character_read(row[0], row[1]) for row in result.all()]

    async def get_character_with_knowledge(
        self,
        character_id: UUID,
    ) -> tuple[
        CharacterRead,
        list[BeliefRead],
        list[RelationshipRead],
        list[GoalRead],
    ]:
        character = await self.get_character(character_id)
        if not character:
            raise ValueError(f"Character {character_id} not found")

        from app.db.repositories.belief_repo import BeliefRepository
        from app.db.repositories.goal_repo import GoalRepository
        from app.db.repositories.relationship_repo import RelationshipRepository

        beliefs = await BeliefRepository(self._session).get_for_character(character_id)
        relationships = await RelationshipRepository(self._session).get_for_character(
            character_id
        )
        goals = await GoalRepository(self._session).get_for_character(character_id)
        return character, beliefs, relationships, goals

    @staticmethod
    def _decode_list(value: str | None) -> list:
        if not value:
            return []
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, list) else []
        except Exception:
            return []

    @staticmethod
    def _decode_dict(value: str | None) -> dict | None:
        if not value:
            return None
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, dict) else None
        except Exception:
            return None

    def _to_entity_read(self, db_entity: Entity) -> EntityRead:
        return EntityRead(
            id=UUID(db_entity.id),
            campaign_id=UUID(db_entity.campaign_id),
            entity_type=db_entity.entity_type,
            canonical_name=db_entity.canonical_name,
            aliases=self._decode_list(db_entity.aliases),
            description=db_entity.description,
            status=db_entity.status,
            provenance=db_entity.provenance,
            version=db_entity.version,
            custom_fields=self._decode_dict(db_entity.custom_fields),
            created_at=db_entity.created_at,
            updated_at=db_entity.updated_at,
        )

    def _to_character_read(
        self,
        db_entity: Entity,
        db_character: Character,
    ) -> CharacterRead:
        entity = self._to_entity_read(db_entity)
        return CharacterRead(
            id=entity.id,
            campaign_id=entity.campaign_id,
            entity_type=entity.entity_type,
            canonical_name=entity.canonical_name,
            aliases=entity.aliases,
            description=entity.description,
            status=entity.status,
            provenance=entity.provenance,
            version=entity.version,
            custom_fields=entity.custom_fields,
            created_at=entity.created_at,
            updated_at=entity.updated_at,
            appearance=db_character.appearance,
            face_description=db_character.face_description,
            body_description=db_character.body_description,
            immutable_features=db_character.immutable_features,
            personality=db_character.personality,
            values=self._decode_list(db_character.values),
            fears=self._decode_list(db_character.fears),
            desires=self._decode_list(db_character.desires),
            voice=db_character.voice,
            speech_patterns=db_character.speech_patterns,
            biography=db_character.biography,
            backstory_public=db_character.backstory_public,
            backstory_secret=db_character.backstory_secret,
            emotional_state=db_character.emotional_state,
            current_location_id=(
                UUID(db_character.current_location_id)
                if db_character.current_location_id
                else None
            ),
            current_intentions=self._decode_list(db_character.current_intentions),
            visual_profile=self._decode_dict(db_character.visual_profile),
        )
