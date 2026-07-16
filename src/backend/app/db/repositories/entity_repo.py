import json
from uuid import UUID
from sqlalchemy import select, delete, or_
from app.db.repositories.base import BaseRepository
from app.db.tables import Entity, Character, Location, Item, Faction, Creature, SceneParticipant, Belief, RelationshipAssertion, CharacterGoal
from app.models.entity import EntityCreate, EntityRead, EntityUpdate, EntityType, EntityStatus
from app.models.character import CharacterCreate, CharacterRead, CharacterUpdate
from app.models.belief import BeliefRead
from app.models.relationship import RelationshipRead
from app.models.goal import GoalRead

class EntityRepository(BaseRepository):
    # --- BASE ENTITY CRUD ---
    async def create(self, campaign_id: UUID, data: EntityCreate) -> EntityRead:
        aliases_str = json.dumps(data.aliases) if data.aliases else json.dumps([])
        custom_fields_str = json.dumps(data.custom_fields) if data.custom_fields else None
        
        db_entity = Entity(
            campaign_id=str(campaign_id),
            entity_type=data.entity_type.value,
            canonical_name=data.canonical_name,
            aliases=aliases_str,
            description=data.description,
            status=data.status.value,
            provenance="manual",
            version=1,
            custom_fields=custom_fields_str
        )
        self._session.add(db_entity)
        await self._session.flush()
        return self._to_entity_read(db_entity)

    async def get_by_id(self, entity_id: UUID) -> EntityRead | None:
        result = await self._session.execute(
            select(Entity).where(Entity.id == str(entity_id))
        )
        db_entity = result.scalar_one_or_none()
        if not db_entity:
            return None
        return self._to_entity_read(db_entity)

    async def list_by_campaign(self, campaign_id: UUID, entity_type: str | None = None) -> list[EntityRead]:
        query = select(Entity).where(Entity.campaign_id == str(campaign_id))
        if entity_type:
            query = query.where(Entity.entity_type == entity_type)
        result = await self._session.execute(query)
        entities = result.scalars().all()
        return [self._to_entity_read(e) for e in entities]

    async def update(self, entity_id: UUID, data: EntityUpdate) -> EntityRead | None:
        result = await self._session.execute(
            select(Entity).where(Entity.id == str(entity_id))
        )
        db_entity = result.scalar_one_or_none()
        if not db_entity:
            return None
            
        update_data = data.model_dump(exclude_unset=True)
        for key, value in update_data.items():
            if key == "aliases" and value is not None:
                setattr(db_entity, key, json.dumps(value))
            elif key == "custom_fields" and value is not None:
                setattr(db_entity, key, json.dumps(value))
            elif key == "status" and value is not None:
                setattr(db_entity, key, value.value)
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

    async def search_by_name(self, campaign_id: UUID, query: str) -> list[EntityRead]:
        """Search canonical name and aliases (fuzzy search in sqlite using LIKE)."""
        search_pattern = f"%{query}%"
        result = await self._session.execute(
            select(Entity)
            .where(
                Entity.campaign_id == str(campaign_id),
                or_(
                    Entity.canonical_name.like(search_pattern),
                    Entity.aliases.like(search_pattern)
                )
            )
        )
        entities = result.scalars().all()
        return [self._to_entity_read(e) for e in entities]

    # --- CHARACTER SPECIALIZED CRUD ---
    async def create_character(self, campaign_id: UUID, data: CharacterCreate) -> CharacterRead:
        # 1. Create base entity
        aliases_str = json.dumps(data.aliases) if data.aliases else json.dumps([])
        custom_fields_str = json.dumps(data.custom_fields) if data.custom_fields else None
        
        db_entity = Entity(
            campaign_id=str(campaign_id),
            entity_type=EntityType.CHARACTER.value,
            canonical_name=data.canonical_name,
            aliases=aliases_str,
            description=data.description,
            status=data.status.value,
            provenance="manual",
            version=1,
            custom_fields=custom_fields_str
        )
        self._session.add(db_entity)
        await self._session.flush()

        # 2. Create character specific row
        db_char = Character(
            entity_id=db_entity.id,
            appearance=data.appearance,
            face_description=data.face_description,
            body_description=data.body_description,
            immutable_features=data.immutable_features,
            personality=data.personality,
            values=json.dumps(data.values) if data.values else json.dumps([]),
            fears=json.dumps(data.fears) if data.fears else json.dumps([]),
            desires=json.dumps(data.desires) if data.desires else json.dumps([]),
            voice=data.voice,
            speech_patterns=data.speech_patterns,
            biography=data.biography,
            backstory_public=data.backstory_public,
            backstory_secret=data.backstory_secret,
            emotional_state=data.emotional_state,
            current_location_id=str(data.current_location_id) if data.current_location_id else None,
            current_intentions=json.dumps(data.current_intentions) if data.current_intentions else json.dumps([]),
            visual_profile=json.dumps(data.visual_profile) if data.visual_profile else None
        )
        self._session.add(db_char)
        await self._session.flush()
        return self._to_character_read(db_entity, db_char)

    async def get_character(self, entity_id: UUID) -> CharacterRead | None:
        result = await self._session.execute(
            select(Entity, Character)
            .join(Character, Entity.id == Character.entity_id)
            .where(Entity.id == str(entity_id))
        )
        row = result.first()
        if not row:
            return None
        db_entity, db_char = row
        return self._to_character_read(db_entity, db_char)

    async def update_character(self, entity_id: UUID, data: CharacterUpdate) -> CharacterRead | None:
        # First update base entity fields
        base_update = EntityUpdate(
            canonical_name=data.canonical_name,
            aliases=data.aliases,
            description=data.description,
            status=data.status,
            custom_fields=data.custom_fields
        )
        await self.update(entity_id, base_update)

        # Get character specific row
        result = await self._session.execute(
            select(Character).where(Character.entity_id == str(entity_id))
        )
        db_char = result.scalar_one_or_none()
        if not db_char:
            return None

        # Update character fields
        update_dict = data.model_dump(exclude_unset=True)
        character_specific_keys = [
            "appearance", "face_description", "body_description", "immutable_features",
            "personality", "values", "fears", "desires", "voice", "speech_patterns",
            "biography", "backstory_public", "backstory_secret", "emotional_state",
            "current_location_id", "current_intentions", "visual_profile"
        ]
        for key in character_specific_keys:
            if key in update_dict:
                value = update_dict[key]
                if key in ["values", "fears", "desires", "current_intentions"] and value is not None:
                    setattr(db_char, key, json.dumps(value))
                elif key == "visual_profile" and value is not None:
                    setattr(db_char, key, json.dumps(value))
                elif key == "current_location_id" and value is not None:
                    setattr(db_char, key, str(value))
                else:
                    setattr(db_char, key, value)

        await self._session.flush()
        return await self.get_character(entity_id)

    async def get_characters_in_scene(self, scene_id: UUID) -> list[CharacterRead]:
        result = await self._session.execute(
            select(Entity, Character)
            .join(Character, Entity.id == Character.entity_id)
            .join(SceneParticipant, Entity.id == SceneParticipant.entity_id)
            .where(SceneParticipant.scene_id == str(scene_id))
        )
        rows = result.all()
        return [self._to_character_read(r[0], r[1]) for r in rows]

    async def get_character_with_knowledge(self, character_id: UUID) -> tuple[CharacterRead, list[BeliefRead], list[RelationshipRead], list[GoalRead]]:
        """Returns character info, active beliefs, current relationships and active goals."""
        character_read = await self.get_character(character_id)
        if not character_read:
            raise ValueError(f"Character {character_id} not found")

        # Beliefs
        from app.db.repositories.belief_repo import BeliefRepository
        belief_repo = BeliefRepository(self._session)
        beliefs = await belief_repo.get_for_character(character_id)

        # Relationships (as subject)
        from app.db.repositories.relationship_repo import RelationshipRepository
        rel_repo = RelationshipRepository(self._session)
        relationships = await rel_repo.get_for_character(character_id)

        # Goals
        from app.db.repositories.goal_repo import GoalRepository
        goal_repo = GoalRepository(self._session)
        goals = await goal_repo.get_for_character(character_id)

        return character_read, beliefs, relationships, goals

    # --- HELPERS ---
    def _to_entity_read(self, db_entity: Entity) -> EntityRead:
        aliases = []
        if db_entity.aliases:
            try:
                aliases = json.loads(db_entity.aliases)
            except Exception:
                pass
        custom_fields = None
        if db_entity.custom_fields:
            try:
                custom_fields = json.loads(db_entity.custom_fields)
            except Exception:
                pass
                
        return EntityRead(
            id=UUID(db_entity.id),
            campaign_id=UUID(db_entity.campaign_id),
            entity_type=db_entity.entity_type,
            canonical_name=db_entity.canonical_name,
            aliases=aliases,
            description=db_entity.description,
            status=db_entity.status,
            provenance=db_entity.provenance,
            version=db_entity.version,
            custom_fields=custom_fields,
            created_at=db_entity.created_at,
            updated_at=db_entity.updated_at
        )

    def _to_character_read(self, db_entity: Entity, db_char: Character) -> CharacterRead:
        entity_read = self._to_entity_read(db_entity)
        
        # Parse JSON fields of Character
        values = []
        if db_char.values:
            try:
                values = json.loads(db_char.values)
            except Exception:
                pass
        fears = []
        if db_char.fears:
            try:
                fears = json.loads(db_char.fears)
            except Exception:
                pass
        desires = []
        if db_char.desires:
            try:
                desires = json.loads(db_char.desires)
            except Exception:
                pass
        current_intentions = []
        if db_char.current_intentions:
            try:
                current_intentions = json.loads(db_char.current_intentions)
            except Exception:
                pass
        visual_profile = None
        if db_char.visual_profile:
            try:
                visual_profile = json.loads(db_char.visual_profile)
            except Exception:
                pass

        current_loc_id = UUID(db_char.current_location_id) if db_char.current_location_id else None

        return CharacterRead(
            id=entity_read.id,
            campaign_id=entity_read.campaign_id,
            entity_type=entity_read.entity_type,
            canonical_name=entity_read.canonical_name,
            aliases=entity_read.aliases,
            description=entity_read.description,
            status=entity_read.status,
            provenance=entity_read.provenance,
            version=entity_read.version,
            custom_fields=entity_read.custom_fields,
            created_at=entity_read.created_at,
            updated_at=entity_read.updated_at,
            
            # Character specific
            appearance=db_char.appearance,
            face_description=db_char.face_description,
            body_description=db_char.body_description,
            immutable_features=db_char.immutable_features,
            personality=db_char.personality,
            values=values,
            fears=fears,
            desires=desires,
            voice=db_char.voice,
            speech_patterns=db_char.speech_patterns,
            biography=db_char.biography,
            backstory_public=db_char.backstory_public,
            backstory_secret=db_char.backstory_secret,
            emotional_state=db_char.emotional_state,
            current_location_id=current_loc_id,
            current_intentions=current_intentions,
            visual_profile=visual_profile
        )
