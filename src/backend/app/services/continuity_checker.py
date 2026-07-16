from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.scene_repo import SceneRepository
from app.models.proposed_change import ChangeType, ProposedChangeCreate


class ContinuityChecker:
    """Perform deterministic validation of structured canon changes."""

    def __init__(self, session: AsyncSession):
        self._session = session
        self._entity_repo = EntityRepository(session)
        self._scene_repo = SceneRepository(session)

    @staticmethod
    def _parse_uuid(value: object, field_name: str) -> tuple[UUID | None, str | None]:
        if value is None or value == "":
            return None, None
        try:
            return UUID(str(value)), None
        except (ValueError, TypeError, AttributeError):
            return None, f"{field_name} must be a UUID, got {value!r}"

    async def validate_change(
        self,
        campaign_id: UUID,
        change: ProposedChangeCreate,
    ) -> tuple[bool, str | None]:
        payload = change.payload
        change_type = change.change_type

        if change_type == ChangeType.FACT:
            subject = payload.get("subject")
            predicate = payload.get("predicate")
            if not subject or not predicate:
                return False, "Fact proposal requires subject and predicate"

            # Text subjects/objects are allowed. UUID-looking values must resolve.
            for field_name in ("subject", "object_value"):
                candidate = payload.get(field_name)
                if not candidate:
                    continue
                try:
                    entity_id = UUID(str(candidate))
                except (ValueError, TypeError, AttributeError):
                    continue
                entity = await self._entity_repo.get_by_id(entity_id)
                if not entity:
                    return False, f"Fact references missing entity ID: {candidate}"
                if entity.status in {"dead", "destroyed"}:
                    return False, (
                        f"Fact references inactive entity: "
                        f"{entity.canonical_name} ({entity.status})"
                    )

        elif change_type == ChangeType.EVENT:
            if not payload.get("description"):
                return False, "Event proposal requires description"

            location_value = payload.get("location_id")
            if location_value:
                location_id, error = self._parse_uuid(location_value, "location_id")
                if error:
                    return False, error
                location = await self._entity_repo.get_by_id(location_id)
                if not location:
                    return False, f"Event references missing location ID: {location_value}"
                if location.entity_type != "location":
                    return False, "Event location_id does not reference a location"

            for participant_value in payload.get("participant_ids", []):
                participant_id, error = self._parse_uuid(
                    participant_value,
                    "participant_id",
                )
                if error:
                    return False, error
                participant = await self._entity_repo.get_by_id(participant_id)
                if not participant:
                    return False, (
                        f"Event references missing participant ID: {participant_value}"
                    )
                if participant.status in {"dead", "destroyed"}:
                    return False, (
                        f"Event participant is inactive: {participant.canonical_name}"
                    )

        elif change_type == ChangeType.RELATIONSHIP:
            subject_id, subject_error = self._parse_uuid(
                payload.get("subject_id"),
                "subject_id",
            )
            object_id, object_error = self._parse_uuid(
                payload.get("object_id"),
                "object_id",
            )
            if subject_error or object_error:
                return False, subject_error or object_error
            if not subject_id or not object_id:
                return False, "Relationship requires subject_id and object_id"
            if subject_id == object_id:
                return False, "Entity cannot have a relationship with itself"
            if not payload.get("relation_type") or not payload.get("description"):
                return False, "Relationship requires relation_type and description"

            subject = await self._entity_repo.get_by_id(subject_id)
            object_entity = await self._entity_repo.get_by_id(object_id)
            if not subject or not object_entity:
                return False, "Relationship references missing entities"
            if subject.status in {"dead", "destroyed"} or object_entity.status in {
                "dead",
                "destroyed",
            }:
                return False, "Relationship references inactive entities"

        elif change_type == ChangeType.MOVEMENT:
            character_id, character_error = self._parse_uuid(
                payload.get("character_id"),
                "character_id",
            )
            location_id, location_error = self._parse_uuid(
                payload.get("location_id"),
                "location_id",
            )
            if character_error or location_error:
                return False, character_error or location_error
            if not character_id or not location_id:
                return False, "Movement requires character_id and location_id"

            character = await self._entity_repo.get_character(character_id)
            location = await self._entity_repo.get_by_id(location_id)
            if not character or not location:
                return False, "Movement references missing character or location"
            if character.status in {"dead", "destroyed"}:
                return False, f"Cannot move inactive character: {character.canonical_name}"
            if location.entity_type != "location":
                return False, (
                    f"Movement target is not a location: "
                    f"{location.canonical_name} ({location.entity_type})"
                )

        elif change_type == ChangeType.SCENE_THESIS:
            scene_id, scene_error = self._parse_uuid(
                payload.get("scene_id"),
                "scene_id",
            )
            if scene_error:
                return False, scene_error
            if not scene_id:
                return False, "Scene thesis requires scene_id"
            if not payload.get("text"):
                return False, "Scene thesis requires text"

            scene = await self._scene_repo.get_by_id(scene_id)
            if not scene:
                return False, f"Scene thesis references missing scene ID: {scene_id}"
            if scene.campaign_id != campaign_id:
                return False, "Scene thesis references a scene from another campaign"

        return True, None
