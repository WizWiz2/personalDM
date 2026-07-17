from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.fact_repo import FactRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.tables import Item
from app.models.proposed_change import ChangeType, ProposedChangeCreate


class ContinuityChecker:
    """Perform deterministic validation of structured canon changes."""

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
            if payload.get("location_id"):
                _, error = await self._entity(
                    campaign_id,
                    payload.get("location_id"),
                    "location_id",
                    "location",
                )
                if error:
                    return False, error
            for participant in payload.get("participant_ids", []):
                _, error = await self._entity(
                    campaign_id,
                    participant,
                    "participant_id",
                )
                if error:
                    return False, error

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
            _, error = await self._entity(
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

        elif change_type == ChangeType.KNOWLEDGE:
            _, error = await self._entity(
                campaign_id,
                payload.get("recipient_id"),
                "recipient_id",
                "character",
            )
            if error:
                return False, error
            if payload.get("source_character_id"):
                _, error = await self._entity(
                    campaign_id,
                    payload.get("source_character_id"),
                    "source_character_id",
                    "character",
                )
                if error:
                    return False, error
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
            if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
                return False, "Knowledge confidence must be between 0 and 1"

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

        elif change_type == ChangeType.SCENE_THESIS:
            scene_id, scene_error = self._parse_uuid(
                payload.get("scene_id"),
                "scene_id",
            )
            if scene_error:
                return False, scene_error
            if not scene_id or not payload.get("text"):
                return False, "Scene thesis requires scene_id and text"
            scene = await self._scene_repo.get_by_id(scene_id)
            if not scene or scene.campaign_id != campaign_id:
                return False, "Scene thesis references another campaign"

        return True, None
