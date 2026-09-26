"""Authorize a planner destination against the location graph.

The intent pipeline already decided that the turn moves and which place it
named. This check does not read travel verbs. A destination is authorized when
it is exactly one persisted location and, if the current place has exits, that
location is one of them or is the current place itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.location_repo import LocationRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.tables import Turn
from app.services.location_identity import same_location_reference
from app.services.scene_state_service import SceneStateService


@dataclass(frozen=True)
class DestinationAuthorization:
    applicable: bool
    authorized: bool
    reason: str
    destination: str
    matched_clause: str | None = None
    destination_exists: bool = False


class PlayerDestinationAuthorizer:
    def __init__(self, session: AsyncSession):
        self._session = session
        self._locations = LocationRepository(session)
        self._scenes = SceneRepository(session)
        self._state = SceneStateService(session)

    async def authorize(
        self,
        trigger_turn_id: UUID | None,
        destination: str | None,
    ) -> DestinationAuthorization:
        clean_destination = " ".join((destination or "").split()).strip(".,;:!?")
        if not trigger_turn_id or not clean_destination:
            return self._unresolved(clean_destination, "no human trigger or destination")

        turn = await self._session.get(Turn, str(trigger_turn_id))
        if not turn or turn.role != "user":
            return self._unresolved(clean_destination, "trigger is not a human user turn")

        locations = await self._locations.list_by_campaign(UUID(turn.campaign_id))
        target = self._match_location(locations, clean_destination)
        if target is None:
            quoted = await self._quoted_in_recent_narration(
                turn,
                clean_destination,
                locations,
            )
            if quoted is None:
                return self._unresolved(
                    clean_destination,
                    "destination does not match one campaign location",
                )
            return self._authorized(
                quoted,
                "destination is quoted in recent narration",
                None,
                False,
            )

        source_id = await self._source_location_id(turn)
        exit_ids = await self._exit_target_ids(UUID(turn.campaign_id), source_id)
        if str(target.id) in exit_ids:
            reason = "destination is an exit from the current location"
        elif source_id is not None and str(target.id) == str(source_id):
            reason = "destination is the current location"
        else:
            reason = "destination matches one campaign location"
        return self._authorized(target.canonical_name, reason, None, True)

    async def _quoted_in_recent_narration(self, turn: Turn, destination: str, locations) -> str | None:
        """A new place is allowed only when its name is already in published narration."""
        source_id = await self._source_location_id(turn)
        source_name = ""
        if source_id is not None:
            source = next((item for item in locations if str(item.id) == str(source_id)), None)
            source_name = source.canonical_name if source else ""
        candidate = destination
        if source_name:
            head, sep, tail = candidate.rpartition("—")
            if sep and tail.strip().casefold() == source_name.casefold():
                candidate = head.strip(" —")
        if not candidate:
            return None
        rows = (
            await self._session.execute(
                select(Turn.content).where(
                    Turn.campaign_id == turn.campaign_id,
                    Turn.role == "assistant",
                )
            )
        ).scalars().all()
        published = "\n".join(row or "" for row in rows).casefold()
        if candidate.casefold() in published:
            return candidate
        return None

    async def _source_location_id(self, turn: Turn) -> UUID | None:
        if not turn.scene_id:
            return None
        try:
            return await self._scenes.get_location_id(UUID(str(turn.scene_id)))
        except (TypeError, ValueError):
            return None

    async def _exit_target_ids(
        self,
        campaign_id: UUID,
        source_location_id: UUID | None,
    ) -> set[str]:
        if source_location_id is None:
            return set()
        exits = await self._state.list_exits(
            campaign_id,
            source_location_id,
            include_hidden=True,
        )
        return {str(row.to_location_id) for row in exits if row.to_location_id}

    @staticmethod
    def _exact_location_match(location, name: str) -> bool:
        needle = name.casefold()
        if location.canonical_name.casefold() == needle:
            return True
        return any(alias.casefold() == needle for alias in location.aliases)

    @classmethod
    def _match_location(cls, locations, name: str):
        for location in locations:
            if cls._exact_location_match(location, name):
                return location
        equivalent = [
            location
            for location in locations
            if same_location_reference(location.canonical_name, name)
            or any(same_location_reference(alias, name) for alias in location.aliases)
        ]
        return equivalent[0] if len(equivalent) == 1 else None

    @staticmethod
    def _authorized(
        destination: str,
        reason: str,
        matched_clause: str | None,
        destination_exists: bool,
    ) -> DestinationAuthorization:
        return DestinationAuthorization(
            applicable=True,
            authorized=True,
            reason=reason,
            destination=destination,
            matched_clause=matched_clause,
            destination_exists=destination_exists,
        )

    @staticmethod
    def _unresolved(
        destination: str,
        reason: str,
        matched_clause: str | None = None,
        *,
        destination_exists: bool = False,
    ) -> DestinationAuthorization:
        return DestinationAuthorization(
            applicable=False,
            authorized=False,
            reason=reason,
            destination=destination,
            matched_clause=matched_clause,
            destination_exists=destination_exists,
        )


__all__ = ["DestinationAuthorization", "PlayerDestinationAuthorizer"]
