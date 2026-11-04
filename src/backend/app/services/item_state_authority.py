from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import Character, Entity, Item
from app.models.item_state import ItemStateSnapshot
from app.services.item_content_authority import revealed_item_contents
from app.services.item_property_authority import current_item_properties


async def load_item_states(session: AsyncSession, campaign_id: UUID) -> list[ItemStateSnapshot]:
    rows = (await session.execute(
        select(Entity, Item).join(Item, Item.entity_id == Entity.id)
        .where(Entity.campaign_id == str(campaign_id), Entity.status != "destroyed")
        .order_by(Entity.canonical_name)
    )).all()
    contents = await revealed_item_contents(session, campaign_id)
    properties = await current_item_properties(session, campaign_id)
    owners = {item.current_owner_id for _, item in rows if item.current_owner_id}
    owner_locations = dict((await session.execute(
        select(Character.entity_id, Character.current_location_id).where(Character.entity_id.in_(owners))
    )).all()) if owners else {}
    references = {value for _, item in rows
                  for value in (item.current_owner_id, item.current_location_id) if value}
    references.update(value for value in owner_locations.values() if value)
    names = dict((await session.execute(
        select(Entity.id, Entity.canonical_name).where(Entity.id.in_(references))
    )).all()) if references else {}
    return [ItemStateSnapshot(
        item_id=entity.id, name=entity.canonical_name,
        owner_id=item.current_owner_id, owner_name=names.get(item.current_owner_id),
        location_id=item.current_location_id, location_name=names.get(item.current_location_id),
        placement_detail=item.placement_detail, quantity=item.quantity,
        owner_location_id=owner_locations.get(item.current_owner_id),
        owner_location_name=names.get(owner_locations.get(item.current_owner_id)),
        revealed_content=contents.get(entity.id, (None, None))[0],
        content_source_step_id=contents.get(entity.id, (None, None))[1],
        properties=properties.get(entity.id, {}),
    ) for entity, item in rows]
