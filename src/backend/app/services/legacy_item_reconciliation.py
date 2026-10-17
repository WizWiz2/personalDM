"""Explicit, audited repair of plot items missed by older prose-only campaigns.

No regex item mining: a GM must choose a canonical item, a physical owner/place,
and an exact excerpt from an ACTIVE published assistant turn. Preview is read-only.
"""
from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.entity_repo import EntityRepository
from app.db.tables import Entity, Item, Turn
from app.models.entity import EntityCreate, EntityType
from app.services.entity_identity import identity_key


class LegacyItemReconciliationRequest(BaseModel):
    canonical_name: str = Field(min_length=2, max_length=120)
    source_turn_id: UUID
    published_evidence: str = Field(min_length=8, max_length=600)
    current_owner_id: UUID | None = None
    current_location_id: UUID | None = None
    placement_detail: str | None = Field(default=None, max_length=300)
    approve: bool = False

    @model_validator(mode="after")
    def require_position(self):
        if bool(self.current_owner_id) == bool(self.current_location_id):
            raise ValueError("Specify exactly one: current_owner_id or current_location_id")
        if self.placement_detail and not self.current_location_id:
            raise ValueError("Placement detail requires a physical location")
        return self


class LegacyItemReconciliation:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.entities = EntityRepository(session)

    async def reconcile(self, campaign_id: UUID, data: LegacyItemReconciliationRequest) -> dict:
        turn = await self.session.get(Turn, str(data.source_turn_id))
        if (
            not turn or turn.campaign_id != str(campaign_id)
            or turn.role != "assistant" or turn.status != "active"
        ):
            raise ValueError("Evidence must be an active published assistant turn")
        normalized_evidence = " ".join(data.published_evidence.split())
        if normalized_evidence.casefold() not in " ".join(turn.content.split()).casefold():
            raise ValueError("Evidence must occur verbatim in the published turn")

        destination = data.current_owner_id or data.current_location_id
        dest = await self.session.get(Entity, str(destination))
        expected_type = "character" if data.current_owner_id else "location"
        if not dest or dest.campaign_id != str(campaign_id) or dest.entity_type != expected_type:
            raise ValueError("Physical destination must be a campaign " + expected_type)

        existing = [
            entity for entity in await self.entities.list_by_campaign(campaign_id, "item")
            if identity_key(data.canonical_name) in {
                identity_key(value) for value in (entity.canonical_name, *entity.aliases)
            }
        ]
        if len(existing) > 1:
            raise ValueError("Several items share this identity; choose a unique canonical name")
        if existing:
            item = await self.session.get(Item, str(existing[0].id))
            if item is None:
                raise ValueError("Existing item has no physical Item record")
            desired_owner = str(data.current_owner_id) if data.current_owner_id else None
            desired_location = str(data.current_location_id) if data.current_location_id else None
            if (
                item.current_owner_id != desired_owner
                or item.current_location_id != desired_location
                or (item.placement_detail or None) != (data.placement_detail or None)
            ):
                raise ValueError("Existing canonical item has different custody; no automatic overwrite")
            return {"status": "already_exists", "item_id": str(existing[0].id)}

        if not data.approve:
            return {
                "status": "preview",
                "item_id": None,
                "canonical_name": data.canonical_name,
                "position": str(destination),
                "placement_detail": data.placement_detail,
                "source_turn_id": str(data.source_turn_id),
                "evidence": normalized_evidence,
                "requires_explicit_approval": True,
            }

        entity = await self.entities.create(
            campaign_id,
            EntityCreate(
                entity_type=EntityType.ITEM,
                canonical_name=data.canonical_name,
                custom_fields={
                    "source": "approved_legacy_item_reconciliation",
                    "source_turn_id": str(data.source_turn_id),
                    "published_evidence": normalized_evidence,
                    "explicit_gm_approval": True,
                },
            ),
        )
        self.session.add(Item(
            entity_id=str(entity.id),
            current_owner_id=str(data.current_owner_id) if data.current_owner_id else None,
            current_location_id=str(data.current_location_id) if data.current_location_id else None,
            placement_detail=data.placement_detail,
            is_unique=True,
        ))
        await self.session.flush()
        return {"status": "created", "item_id": str(entity.id)}
