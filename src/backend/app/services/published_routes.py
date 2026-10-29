"""Publish typed directions atomically with prose; undo only their own graph effects."""
from __future__ import annotations

import json
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.location_repo import LocationRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.scene_state_table import LocationExit
from app.db.tables import Character, Entity
from app.models.location import LocationCreate
from app.models.narration_validation import GrantedBeat
from app.models.turn_authority import TurnAuthority
from app.services.entity_identity import identity_key


async def route_catalog(session: AsyncSession, campaign_id: UUID) -> list[dict]:
    # Some pipeline unit tests use a session stub; runtime always uses AsyncSession.
    if not isinstance(session, AsyncSession):
        return []
    places = await LocationRepository(session).list_by_campaign(campaign_id)
    return [{"id": str(place.id), "name": place.canonical_name, "aliases": place.aliases}
            for place in places if place.status == "active"]


def _edge_state(edge: LocationExit) -> dict:
    return {key: getattr(edge, key) for key in (
        "campaign_id", "from_location_id", "to_location_id", "label", "direction",
        "travel_time", "access_rule", "discovered", "active",
    )}


async def publish_routes(
    session: AsyncSession, authority: TurnAuthority, beat: GrantedBeat | None,
    prose: str, source_turn_id: UUID,
) -> dict:
    """No commit: a failed publication rolls these writes back with the assistant turn."""
    receipt = {"edges": [], "places": []}
    if not beat or not beat.route_disclosures:
        return receipt
    if beat.failure(authority.beat_owner_id, authority.beat_owner_name or "", prose):
        raise ValueError("cannot publish uncertified directions")
    if not authority.target_scene_id:
        raise ValueError("route speaker must be physically present")
    await session.flush()  # Sessions disable autoflush; inspect this publication's final positions.
    origin = await SceneRepository(session).get_location_id(authority.target_scene_id)
    speaker = (await session.execute(select(Character).join(Entity, Entity.id == Character.entity_id).where(
        Entity.campaign_id == str(authority.campaign_id), Entity.status == "active",
        Character.entity_id == str(authority.beat_owner_id), Character.current_location_id == str(origin),
    ))).scalar_one_or_none()
    if speaker is None:
        raise ValueError("route speaker must be physically present")
    places = await LocationRepository(session).list_by_campaign(authority.campaign_id)
    places = [place for place in places if place.status == "active"]
    by_id = {str(place.id): place for place in places}
    if str(origin) not in by_id:
        raise ValueError("route origin is not an active campaign place")
    for route in beat.route_disclosures:
        kind, separator, reference = route.destination.partition(":")
        if not separator or kind not in {"known", "new_in"} or reference not in by_id:
            raise ValueError("route destination must reference an active campaign place")
        matches = [place for place in places
                   if identity_key(route.name) in {identity_key(place.canonical_name),
                                                   *(identity_key(alias) for alias in place.aliases)}]
        if kind == "known":
            destination = by_id[reference]
            if destination not in matches:
                raise ValueError("route name disagrees with its registered destination")
        elif len(matches) == 1:
            destination = matches[0]  # Do not duplicate an existing named place.
        elif matches:
            raise ValueError("new route destination has an ambiguous existing identity")
        else:
            destination = await LocationRepository(session).create(authority.campaign_id, LocationCreate(
                canonical_name=route.name, description=route.profile,
                geography=route.profile, parent_location_id=UUID(reference),
                custom_fields={"published_route_source": str(source_turn_id)},
            ))
            places.append(destination)
            by_id[str(destination.id)] = destination
            receipt["places"].append(str(destination.id))
        if str(destination.id) == str(origin):
            continue
        hops = [(str(origin), str(destination.id), destination.canonical_name)]
        if route.bidirectional:
            hops.append((str(destination.id), str(origin), by_id[str(origin)].canonical_name))
        for start, end, label in hops:
            edge = (await session.execute(select(LocationExit).where(
                LocationExit.campaign_id == str(authority.campaign_id),
                LocationExit.from_location_id == start, LocationExit.to_location_id == end,
            ))).scalar_one_or_none()
            if edge:
                # Directions never erase gates, costs or a physical closure.
                if edge.discovered:
                    continue
                before = _edge_state(edge)
                edge.discovered = True
            else:
                reverse = (await session.execute(select(LocationExit).where(
                    LocationExit.campaign_id == str(authority.campaign_id),
                    LocationExit.from_location_id == end, LocationExit.to_location_id == start,
                ))).scalar_one_or_none()
                if reverse and (not reverse.active or reverse.access_rule):
                    continue  # A new reciprocal edge cannot bypass an established gate.
                before = None
                edge = LocationExit(campaign_id=str(authority.campaign_id),
                    from_location_id=start, to_location_id=end, label=label,
                    discovered=True, active=True)
                session.add(edge)
            await session.flush()
            receipt["edges"].append({"id": edge.id, "before": before, "after": _edge_state(edge)})
    return receipt


async def undo_published_routes(session: AsyncSession, campaign_id: UUID, snapshot: dict,
                                source_turn_id: UUID) -> None:
    receipt = snapshot.get("published_routes") or {}
    for change in reversed(receipt.get("edges") or []):
        edge = await session.get(LocationExit, change["id"])
        if not edge or edge.campaign_id != str(campaign_id) or _edge_state(edge) != change["after"]:
            continue  # Preserve independent edits made after publication.
        if change["before"] is None:
            await session.delete(edge)
        else:
            for key, value in change["before"].items():
                setattr(edge, key, value)
    for place_id in receipt.get("places") or []:
        entity = await session.get(Entity, place_id)
        if entity and entity.campaign_id == str(campaign_id):
            fields = json.loads(entity.custom_fields or "{}")
            if fields.get("published_route_source") == str(source_turn_id):
                # Keep historical references valid, but retract it as a travel destination.
                entity.status = "inactive"
    await session.flush()
