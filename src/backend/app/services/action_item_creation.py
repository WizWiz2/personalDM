"""Materialize and compensate item creation at its executed source step."""
import json

from sqlalchemy import delete, select

from app.db.tables import Entity, Item, ItemContentDisclosure, ItemCreationEffect, ItemPropertyEffect
from app.services.item_content_authority import revealed_item_contents


async def take_units(session, introduction) -> str | None:
    """Move typed units out of their source item by ID; fail closed when it lacks them."""
    if introduction.taken_from_item_id is None:
        return None
    source = await session.get(Item, str(introduction.taken_from_item_id))
    held = source.quantity if source and source.quantity is not None else introduction.source_quantity_before
    if held is None or introduction.quantity > held:
        raise ValueError("Units can only be taken from an item that typed-holds at least that many")
    source.quantity = held - introduction.quantity
    return source.entity_id


async def return_units(session, item_ids) -> None:
    """Undoing a split record gives its units back to the source it was taken from."""
    for item in (await session.execute(select(Item).where(
            Item.entity_id.in_([str(i) for i in item_ids]), Item.split_from_item_id.is_not(None)))).scalars():
        source = await session.get(Item, item.split_from_item_id)
        if source is not None and source.quantity is not None and item.quantity:
            source.quantity += item.quantity


async def create_step_items(session, campaign_id, player_id, location_id, turn_id, step_id, introductions):
    contents = await revealed_item_contents(session, campaign_id) if any(i.copy_of_item_id for i in introductions) else {}
    for introduction in introductions:
        if introduction.reserved_item_id is None:
            raise ValueError("Executable item creation requires a compiler-reserved identity")
        item_id = str(introduction.reserved_item_id)
        session.add(Entity(
            id=item_id, campaign_id=str(campaign_id), entity_type="item",
            canonical_name=introduction.canonical_name, aliases="[]", description=introduction.description,
            status="active", provenance="turn_authority", version=1,
            custom_fields=json.dumps({"introduced_by": "turn_authority", "introduction_turn_id": str(turn_id),
                                      "introduction_evidence": introduction.evidence}, ensure_ascii=False),
        ))
        await session.flush()
        session.add(Item(
            entity_id=item_id,
            current_owner_id=str(player_id) if introduction.owned_by_player else None,
            current_location_id=None if introduction.owned_by_player else str(location_id),
            placement_detail=introduction.placement_detail, is_unique=True,
            quantity=introduction.quantity, split_from_item_id=await take_units(session, introduction),
        ))
        await session.flush()
        session.add(ItemCreationEffect(source_step_id=str(step_id), item_id=item_id,
            owner_id=str(player_id) if introduction.owned_by_player else None,
            location_id=None if introduction.owned_by_player else str(location_id),
            placement_detail=introduction.placement_detail))
        content = (contents[str(introduction.copy_of_item_id)][0]
                   if str(introduction.copy_of_item_id) in contents else introduction.written_content)
        if content is not None:
            session.add(ItemContentDisclosure(source_step_id=str(step_id), item_id=item_id, content=content))
    await session.flush()


async def undo_step_items(session, source_step_id):
    ids = list((await session.execute(select(ItemCreationEffect.item_id).where(
        ItemCreationEffect.source_step_id == str(source_step_id)
    ))).scalars())
    if not ids:
        return
    await return_units(session, ids)
    # Explicit deletion also supports SQLite installations without FK cascades.
    for model in (ItemContentDisclosure, ItemPropertyEffect, ItemCreationEffect):
        await session.execute(delete(model).where(model.item_id.in_(ids)))
    await session.execute(delete(Item).where(Item.entity_id.in_(ids)))
    await session.execute(delete(Entity).where(Entity.id.in_(ids)))
