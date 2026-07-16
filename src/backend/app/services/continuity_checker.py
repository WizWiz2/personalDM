from uuid import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.scene_repo import SceneRepository
from app.models.proposed_change import ProposedChangeCreate, ChangeType

class ContinuityChecker:
    """Performs deterministic semantic validation on proposed changes (ADR-007 / ADR-008)."""
    
    def __init__(self, session: AsyncSession):
        self._session = session
        self._entity_repo = EntityRepository(session)
        self._scene_repo = SceneRepository(session)

    async def validate_change(self, campaign_id: UUID, change: ProposedChangeCreate) -> tuple[bool, str | None]:
        """Validates a proposed change against current DB constraints and status.

        Returns:
            bool: True if valid, False if it violates continuity.
            str | None: Warning message describing the violation, or None.
        """
        payload = change.payload
        change_type = change.change_type
        
        try:
            if change_type == ChangeType.FACT:
                # payload: { "subject": "uuid or text", "predicate": "text", "object_value": "uuid or text" }
                # In MVP, check if subject/object look like UUIDs, and if so, check if they exist and are active
                subject_id = payload.get("subject")
                object_id = payload.get("object_value")
                
                for id_candidate in [subject_id, object_id]:
                    if id_candidate and self._is_uuid(id_candidate):
                        entity = await self._entity_repo.get_by_id(UUID(id_candidate))
                        if not entity:
                            return False, f"Fact references missing entity ID: {id_candidate}"
                        if entity.status in ["dead", "destroyed"]:
                            return False, f"Fact references inactive/dead entity: {entity.canonical_name} ({entity.status})"

            elif change_type == ChangeType.EVENT:
                # payload: { "event_type": "text", "description": "text", "location_id": "uuid/none", "participant_ids": [...] }
                loc_id = payload.get("location_id")
                if loc_id:
                    location = await self._entity_repo.get_by_id(UUID(loc_id))
                    if not location:
                        return False, f"Event references missing location ID: {loc_id}"
                        
                participant_ids = payload.get("participant_ids", [])
                for p_id in participant_ids:
                    participant = await self._entity_repo.get_by_id(UUID(p_id))
                    if not participant:
                        return False, f"Event references missing participant ID: {p_id}"
                    if participant.status in ["dead", "destroyed"]:
                        return False, f"Event participant is dead/destroyed: {participant.canonical_name}"

            elif change_type == ChangeType.RELATIONSHIP:
                # payload: { "subject_id": "uuid", "object_id": "uuid", "relation_type": "text", "description": "text" }
                sub_id = payload.get("subject_id")
                obj_id = payload.get("object_id")
                if not sub_id or not obj_id:
                    return False, "Relationship proposal missing subject_id or object_id"
                if sub_id == obj_id:
                    return False, "Entity cannot have a relationship with itself"
                    
                sub = await self._entity_repo.get_by_id(UUID(sub_id))
                obj = await self._entity_repo.get_by_id(UUID(obj_id))
                if not sub or not obj:
                    return False, "Relationship references missing entities"
                if sub.status in ["dead", "destroyed"] or obj.status in ["dead", "destroyed"]:
                    return False, "Relationship references inactive or dead entities"

            elif change_type == ChangeType.MOVEMENT:
                # payload: { "character_id": "uuid", "location_id": "uuid" }
                char_id = payload.get("character_id")
                loc_id = payload.get("location_id")
                if not char_id or not loc_id:
                    return False, "Movement missing character_id or location_id"
                    
                char = await self._entity_repo.get_character(UUID(char_id))
                loc = await self._entity_repo.get_by_id(UUID(loc_id))
                if not char or not loc:
                    return False, "Movement references missing character or location"
                if char.status in ["dead", "destroyed"]:
                    return False, f"Cannot move a dead character: {char.canonical_name}"
                if loc.entity_type != "location":
                    return False, f"Movement target is not a location type: {loc.canonical_name} ({loc.entity_type})"

            elif change_type == ChangeType.SCENE_THESIS:
                # payload: { "scene_id": "uuid", "thesis_type": "text", "text": "text" }
                sc_id = payload.get("scene_id")
                if sc_id:
                    scene = await self._scene_repo.get_by_id(UUID(sc_id))
                    if not scene:
                        return False, f"Scene thesis references missing scene ID: {sc_id}"

            return True, None
            
        except Exception as e:
            return False, f"Deterministic validation crashed: {str(e)}"

    def _is_uuid(self, val: str) -> bool:
        try:
            UUID(str(val))
            return True
        except ValueError:
            return False
