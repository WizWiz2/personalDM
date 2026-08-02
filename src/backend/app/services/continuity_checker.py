from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.fact_repo import FactRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.tables import Item
from app.models.memory_semantics import MemoryClass, MemoryMetadata, MemoryRetention
from app.models.proposed_change import ChangeType, ProposedChangeCreate


class ContinuityChecker:
    """Perform deterministic validation of structured canon and memory changes."""

    def __init__(self, session: AsyncSession):
        self._session = session
        self._entity_repo = EntityRepository(session)
        self._fact_repo = FactRepository(session)
        self._scene_repo = SceneRepository(session)

    @staticmethod
    def _parse_uuid(value: object, field_name: str) -> tuple[UUID | None, str | None]:
        if value is None or value == "":
            return None, None
        try:
            return UUID(str(value)), None
        except (ValueError, TypeError, AttributeError):
            return None, f"{field_name} must be a UUID, got {value!r}"

    async def _entity(
        self,
        campaign_id: UUID,
        value: object,
        field_name: str,
        expected_type: str | None = None,
    ):
        entity_id, error = self._parse_uuid(value, field_name)
        if error:
            return None, error
        if not entity_id:
            return None, f"{field_name} is required"
        entity = await self._entity_repo.get_by_id(entity_id)
        if not entity or entity.campaign_id != campaign_id:
            return None, f"{field_name} references an entity outside the campaign"
        if expected_type and entity.entity_type != expected_type:
            return None, f"{field_name} must reference a {expected_type}"
        if entity.status in {"dead", "destroyed"}:
            return None, f"{field_name} references inactive entity {entity.canonical_name}"
        return entity, None

    async def _scene_context(
        self,
        campaign_id: UUID,
        scene_id: UUID | None,
    ):
        if not scene_id:
            return None, set(), None
        scene = await self._scene_repo.get_by_id(scene_id)
        if not scene or scene.campaign_id != campaign_id:
            return None, set(), "scene_id references a scene outside the campaign"
        return scene, set(scene.participants), None

    @staticmethod
    def _canon_metadata(payload: dict) -> tuple[dict, str | None]:
        metadata = payload.get("_canon")
        if metadata is None:
            return {}, None
        if not isinstance(metadata, dict):
            return {}, "_canon must be an object"
        authority = metadata.get("authority")
        if authority not in {
            "dm_confirmed",
            "public_observation",
            "character_claim",
            "player_intent",
        }:
            return metadata, "_canon.authority is invalid"
        if not metadata.get("outcome_id") or not metadata.get("evidence"):
            return metadata, "_canon requires outcome_id and evidence"
        operation = metadata.get("operation", payload.get("operation", "assert"))
        if operation not in {"assert", "revise", "retract", "contradict"}:
            return metadata, "_canon.operation is invalid"
        return metadata, None

    @staticmethod
    def _legacy_memory(change_type: ChangeType, payload: dict) -> MemoryMetadata:
        if change_type == ChangeType.NARRATIVE_DETAIL:
            return MemoryMetadata(
                memory_class=MemoryClass.NARRATIVE_DETAIL,
                retention=MemoryRetention.RECENT_TURNS,
                ttl_turns=payload.get("ttl_turns", 3),
            )
        if change_type == ChangeType.FACT:
            if payload.get("scope") == "scene":
                return MemoryMetadata(
                    memory_class=MemoryClass.SCENE_STATE,
                    retention=MemoryRetention.SCENE_LIFETIME,
                )
            return MemoryMetadata(
                memory_class=MemoryClass.WORLD_CANON,
                retention=MemoryRetention.DURABLE,
            )
        if change_type == ChangeType.EVENT:
            return MemoryMetadata(
                memory_class=MemoryClass.WORLD_CANON,
                retention=MemoryRetention.DURABLE,
            )
        return MemoryMetadata(
            memory_class=MemoryClass.ENTITY_STATE,
            retention=MemoryRetention.UNTIL_SUPERSEDED,
        )

    @classmethod
    def _memory_metadata(
        cls,
        change_type: ChangeType,
        payload: dict,
    ) -> tuple[MemoryMetadata | None, str | None]:
        raw = payload.get("_memory")
        if raw is None:
            try:
                return cls._legacy_memory(change_type, payload), None
            except ValidationError as exc:
                return None, str(exc)
        if not isinstance(raw, dict):
            return None, "_memory must be an object"
        try:
            return (
                MemoryMetadata(
                    memory_class=raw.get("class"),
                    retention=raw.get("retention"),
                    ttl_turns=raw.get("ttl_turns"),
                ),
                None,
            )
        except ValidationError as exc:
            return None, f"Invalid memory lifecycle: {exc}"

    @staticmethod
    def _expected_memory(change_type: ChangeType) -> set[MemoryClass]:
        if change_type == ChangeType.FACT:
            return {
                MemoryClass.WORLD_CANON,
                MemoryClass.ENTITY_STATE,
                MemoryClass.SCENE_STATE,
            }
        if change_type == ChangeType.EVENT:
            return {MemoryClass.WORLD_CANON}
        if change_type == ChangeType.NARRATIVE_DETAIL:
            return {MemoryClass.NARRATIVE_DETAIL}
        if change_type in {
            ChangeType.RELATIONSHIP,
            ChangeType.MOVEMENT,
            ChangeType.KNOWLEDGE,
            ChangeType.ITEM_TRANSFER,
        }:
            return {MemoryClass.ENTITY_STATE}
        return set(MemoryClass)

    async def validate_change(
        self,
        campaign_id: UUID,
        change: ProposedChangeCreate,
        *,
        scene_id: UUID | None = None,
    ) -> tuple[bool, str | None]:
        payload = change.payload
        change_type = change.change_type

        if change_type == ChangeType.CANON_GAP:
            return False, payload.get("_validation_error") or "Uncovered durable canon outcome"

        canon, canon_error = self._canon_metadata(payload)
        if canon_error:
            return False, canon_error
        if canon.get("authority") == "player_intent":
            return False, "Player intent cannot directly create memory"
        if canon.get("authority") == "character_claim" and change_type != ChangeType.KNOWLEDGE:
            return False, "Character claims may create knowledge, not objective world memory"

        memory, memory_error = self._memory_metadata(change_type, payload)
        if memory_error or memory is None:
            return False, memory_error or "Memory lifecycle is missing"
        if memory.memory_class not in self._expected_memory(change_type):
            return (
                False,
                f"{change_type.value} cannot use memory class {memory.memory_class.value}",
            )

        scene, scene_participants, scene_error = await self._scene_context(
            campaign_id,
            scene_id,
        )
        if scene_error:
            return False, scene_error

        if change_type == ChangeType.FACT:
            subject = payload.get("subject")
            predicate = payload.get("predicate")
            if not subject or not predicate:
                return False, "Fact proposal requires subject and predicate"
            operation = payload.get("operation", canon.get("operation", "assert"))
            cardinality = payload.get("cardinality", canon.get("cardinality", "single"))
            if operation not in {"assert", "revise", "retract", "contradict"}:
                return False, "Fact operation must be assert, revise, retract or contradict"
            if cardinality not in {"single", "multi"}:
                return False, "Fact cardinality must be single or multi"
            if operation != "retract" and payload.get("object_value") is None:
                return False, "Non-retraction fact requires object_value"

            scope = payload.get("scope", "campaign")
            if memory.memory_class == MemoryClass.WORLD_CANON and scope != "campaign":
                return False, "world_canon fact must use campaign scope"
            if memory.memory_class == MemoryClass.SCENE_STATE:
                if scope != "scene" or not scene_id:
                    return False, "scene_state fact requires the authoritative scene"
                fact_scene_id, error = self._parse_uuid(payload.get("scene_id"), "scene_id")
                if error:
                    return False, error
                if fact_scene_id != scene_id:
                    return False, "scene_state fact must reference the current scene"
            if memory.memory_class == MemoryClass.ENTITY_STATE:
                if scope != "campaign":
                    return False, "entity_state fact persists until superseded and uses campaign scope"
                _, error = await self._entity(
                    campaign_id,
                    payload.get("subject_entity_id"),
                    "subject_entity_id",
                )
                if error:
                    return False, error

            for field_name in ("subject", "object_value"):
                candidate = payload.get(field_name)
                if not candidate:
                    continue
                try:
                    candidate_id = UUID(str(candidate))
                except (ValueError, TypeError, AttributeError):
                    continue
                entity = await self._entity_repo.get_by_id(candidate_id)
                if not entity or entity.campaign_id != campaign_id:
                    return False, f"Fact {field_name} references another campaign"
                if entity.status in {"dead", "destroyed"}:
                    return False, f"Fact references inactive entity {entity.canonical_name}"

        elif change_type == ChangeType.EVENT:
            if not payload.get("description"):
                return False, "Event proposal requires description"
            event_location = None
            if payload.get("location_id"):
                event_location, error = await self._entity(
                    campaign_id,
                    payload.get("location_id"),
                    "location_id",
                    "location",
                )
                if error:
                    return False, error
            if (
                scene
                and scene.location_id
                and event_location
                and event_location.id != scene.location_id
            ):
                return False, "Event location differs from the authoritative scene location"
            for participant in payload.get("participant_ids", []):
                entity, error = await self._entity(
                    campaign_id,
                    participant,
                    "participant_id",
                )
                if error:
                    return False, error
                if (
                    scene
                    and entity.entity_type == "character"
                    and entity.id not in scene_participants
                ):
                    return False, (
                        f"Event participant {entity.canonical_name} is not physically "
                        "present in the authoritative scene"
                    )

        elif change_type == ChangeType.RELATIONSHIP:
            subject, error = await self._entity(
                campaign_id,
                payload.get("subject_id"),
                "subject_id",
            )
            if error:
                return False, error
            object_entity, error = await self._entity(
                campaign_id,
                payload.get("object_id"),
                "object_id",
            )
            if error:
                return False, error
            if subject.id == object_entity.id:
                return False, "Entity cannot have a relationship with itself"
            if not payload.get("relation_type") or not payload.get("description"):
                return False, "Relationship requires relation_type and description"

        elif change_type == ChangeType.MOVEMENT:
            character_entity, error = await self._entity(
                campaign_id,
                payload.get("character_id"),
                "character_id",
                "character",
            )
            if error:
                return False, error
            _, error = await self._entity(
                campaign_id,
                payload.get("location_id"),
                "location_id",
                "location",
            )
            if error:
                return False, error
            if scene:
                character = await self._entity_repo.get_character(character_entity.id)
                starts_in_scene = character_entity.id in scene_participants
                if (
                    not starts_in_scene
                    and scene.location_id
                    and character
                    and character.current_location_id == scene.location_id
                ):
                    starts_in_scene = True
                if not starts_in_scene:
                    return False, (
                        f"Character {character_entity.canonical_name} cannot move from "
                        "a scene where they are not physically present"
                    )

        elif change_type == ChangeType.KNOWLEDGE:
            recipient, error = await self._entity(
                campaign_id,
                payload.get("recipient_id"),
                "recipient_id",
                "character",
            )
            if error:
                return False, error
            source = None
            if payload.get("source_character_id"):
                source, error = await self._entity(
                    campaign_id,
                    payload.get("source_character_id"),
                    "source_character_id",
                    "character",
                )
                if error:
                    return False, error
            if source and source.id == recipient.id:
                return False, "Character cannot learn a claim from itself"
            fact_id, fact_error = self._parse_uuid(payload.get("fact_id"), "fact_id")
            if fact_error:
                return False, fact_error
            if fact_id:
                fact = await self._fact_repo.get_by_id(fact_id)
                if not fact or fact.campaign_id != campaign_id or not fact.is_current:
                    return False, "Knowledge references a missing or stale fact"
            if not fact_id and not payload.get("proposition"):
                return False, "Knowledge requires fact_id or proposition"
            confidence = payload.get("confidence", 1.0)
            if not isinstance(confidence, (int, float)) or not 0 < confidence <= 1:
                return False, "Knowledge confidence must be greater than 0 and at most 1"

        elif change_type == ChangeType.ITEM_TRANSFER:
            item_entity, error = await self._entity(
                campaign_id,
                payload.get("item_id"),
                "item_id",
                "item",
            )
            if error:
                return False, error
            owner_id = payload.get("owner_id")
            location_id = payload.get("location_id")
            if owner_id and location_id:
                return False, "Item can have an owner or a location, not both"
            if owner_id:
                _, error = await self._entity(campaign_id, owner_id, "owner_id")
                if error:
                    return False, error
            if location_id:
                _, error = await self._entity(
                    campaign_id,
                    location_id,
                    "location_id",
                    "location",
                )
                if error:
                    return False, error
            result = await self._session.execute(
                select(Item).where(Item.entity_id == str(item_entity.id))
            )
            if not result.scalar_one_or_none():
                return False, "Item has no item-state row"

        elif change_type == ChangeType.NARRATIVE_DETAIL:
            if not scene or not scene_id:
                return False, "Narrative detail requires an active scene"
            if not payload.get("text"):
                return False, "Narrative detail requires text"
            ttl_turns = payload.get("ttl_turns", memory.ttl_turns or 3)
            if not isinstance(ttl_turns, int) or not 1 <= ttl_turns <= 8:
                return False, "Narrative detail ttl_turns must be between 1 and 8"
            salience = payload.get("salience", 0.5)
            if not isinstance(salience, (int, float)) or not 0 <= salience <= 1:
                return False, "Narrative detail salience must be between 0 and 1"
            for participant in payload.get("participant_ids", []):
                entity, error = await self._entity(
                    campaign_id,
                    participant,
                    "participant_id",
                )
                if error:
                    return False, error
                if entity.entity_type == "character" and entity.id not in scene_participants:
                    return False, (
                        f"Narrative detail references absent character {entity.canonical_name}"
                    )

        elif change_type == ChangeType.SCENE_THESIS:
            thesis_scene_id, scene_error = self._parse_uuid(
                payload.get("scene_id"),
                "scene_id",
            )
            if scene_error:
                return False, scene_error
            if not thesis_scene_id or not payload.get("text"):
                return False, "Scene thesis requires scene_id and text"
            thesis_scene = await self._scene_repo.get_by_id(thesis_scene_id)
            if not thesis_scene or thesis_scene.campaign_id != campaign_id:
                return False, "Scene thesis references another campaign"

        return True, None
