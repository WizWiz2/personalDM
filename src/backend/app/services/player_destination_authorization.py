from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.location_repo import LocationRepository
from app.db.tables import Turn


@dataclass(frozen=True)
class DestinationAuthorization:
    applicable: bool
    authorized: bool
    reason: str
    destination: str
    matched_clause: str | None = None


class PlayerDestinationAuthorizer:
    """Authorize planner destinations from the player's actual travel clauses.

    The planner may propose a destination, but it cannot make an ambiguous or
    unrelated location choice authoritative. This service independently checks
    the persisted human input and keeps travel-clause scope deterministic.
    """

    TOKEN_RE = re.compile(r"[a-zа-яё0-9]+", re.IGNORECASE)
    TRAVEL_ANCHOR_RE = re.compile(
        r"\b(?:"
        r"go|going|return|returning|enter|entering|head|heading|travel|traveling|"
        r"travelling|drive|driving|ride|riding|get|getting|walk|walking|step|stepping|"
        r"make\s+(?:my|our|their|his|her)\s+way|"
        r"иду|пойду|еду|поеду|отправляюсь|направляюсь|возвращаюсь|вхожу|захожу|"
        r"зайду|выхожу|выезжаю|добираюсь|доберусь|следую|спускаюсь|поднимаюсь"
        r")\b",
        re.IGNORECASE,
    )
    ELLIPTICAL_TRAVEL_RE = re.compile(
        r"^(?:back\s+)?(?:to|into|toward|towards)\b|"
        r"^(?:обратно\s+)?(?:в|во|на|к|до)\b",
        re.IGNORECASE,
    )
    CHAIN_SEPARATOR_RE = re.compile(
        r"\s*(?:,|;|\b(?:and\s+then|then|затем|потом|после\s+этого)\b)\s*",
        re.IGNORECASE,
    )
    CONJUNCTION_BEFORE_TRAVEL_RE = re.compile(
        r"\s+\b(?:and|и|а)\b\s+(?=" + TRAVEL_ANCHOR_RE.pattern + r")",
        re.IGNORECASE,
    )
    GENERIC_LOCATION_TOKENS = frozenset(
        {
            "бар",
            "дом",
            "здание",
            "квартира",
            "комната",
            "офис",
            "улица",
            "район",
            "квартал",
            "департамент",
            "вход",
            "подвал",
            "тоннель",
            "зал",
            "рынок",
            "door",
            "room",
            "street",
            "district",
            "office",
            "apartment",
            "building",
            "department",
            "entrance",
            "basement",
            "tunnel",
            "hall",
            "quarter",
            "market",
            "bar",
        }
    )

    def __init__(self, session: AsyncSession):
        self._session = session
        self._locations = LocationRepository(session)

    async def authorize(
        self,
        trigger_turn_id: UUID | None,
        destination: str | None,
    ) -> DestinationAuthorization:
        clean_destination = " ".join((destination or "").split())
        if not trigger_turn_id or not clean_destination:
            return DestinationAuthorization(
                applicable=False,
                authorized=False,
                reason="no human trigger or destination",
                destination=clean_destination,
            )

        turn = await self._session.get(Turn, str(trigger_turn_id))
        if not turn or turn.role != "user":
            return DestinationAuthorization(
                applicable=False,
                authorized=False,
                reason="trigger is not a human user turn",
                destination=clean_destination,
            )

        clauses = self._travel_clauses(turn.content or "")
        if not clauses:
            return DestinationAuthorization(
                applicable=True,
                authorized=False,
                reason="player input contains no explicit travel clause",
                destination=clean_destination,
            )

        locations = await self._locations.list_by_campaign(UUID(turn.campaign_id))
        target = self._match_location(locations, clean_destination)
        ambiguous_generic = False
        for clause in clauses:
            specific_match, generic_matches = self._destination_reference(
                clause,
                clean_destination,
            )
            if specific_match:
                return DestinationAuthorization(
                    applicable=True,
                    authorized=True,
                    reason="destination is specifically named in a travel clause",
                    destination=clean_destination,
                    matched_clause=clause,
                )
            if not generic_matches:
                continue

            compatible = [
                location
                for location in locations
                if self._location_matches_generic(location.canonical_name, generic_matches)
            ]
            if len(compatible) == 1 and target and compatible[0].id == target.id:
                return DestinationAuthorization(
                    applicable=True,
                    authorized=True,
                    reason="generic travel reference resolves to one known location",
                    destination=clean_destination,
                    matched_clause=clause,
                )
            if len(compatible) > 1:
                ambiguous_generic = True

        reason = (
            "player destination reference is ambiguous"
            if ambiguous_generic
            else "planner destination is not selected by any player travel clause"
        )
        return DestinationAuthorization(
            applicable=True,
            authorized=False,
            reason=reason,
            destination=clean_destination,
        )

    @classmethod
    def _travel_clauses(cls, text: str) -> list[str]:
        normalized = " ".join(text.casefold().split())
        if not normalized:
            return []

        coarse = [part.strip() for part in cls.CHAIN_SEPARATOR_RE.split(normalized)]
        parts: list[str] = []
        for part in coarse:
            if not part:
                continue
            parts.extend(
                piece.strip()
                for piece in cls.CONJUNCTION_BEFORE_TRAVEL_RE.split(part)
                if piece.strip()
            )

        result: list[str] = []
        previous_was_travel = False
        for part in parts:
            explicit = bool(cls.TRAVEL_ANCHOR_RE.search(part))
            elliptical = previous_was_travel and bool(cls.ELLIPTICAL_TRAVEL_RE.search(part))
            is_travel = explicit or elliptical
            if is_travel:
                result.append(part)
            previous_was_travel = is_travel
        return result

    @classmethod
    def _destination_reference(
        cls,
        clause: str,
        destination: str,
    ) -> tuple[bool, set[str]]:
        clause_tokens = cls.TOKEN_RE.findall(clause.casefold())
        destination_tokens = cls.TOKEN_RE.findall(destination.casefold())
        if not clause_tokens or not destination_tokens:
            return False, set()

        specific = [
            token
            for token in destination_tokens
            if len(token) >= 3 and token not in cls.GENERIC_LOCATION_TOKENS
        ]
        if any(
            cls._tokens_match(target, source)
            for target in specific
            for source in clause_tokens
        ):
            return True, set()

        generic_matches = {
            target
            for target in destination_tokens
            if target in cls.GENERIC_LOCATION_TOKENS
            and any(cls._tokens_match(target, source) for source in clause_tokens)
        }
        return False, generic_matches

    @classmethod
    def _location_matches_generic(cls, name: str, generic_matches: set[str]) -> bool:
        tokens = cls.TOKEN_RE.findall(name.casefold())
        return any(
            cls._tokens_match(generic, token)
            for generic in generic_matches
            for token in tokens
        )

    @staticmethod
    def _tokens_match(left: str, right: str) -> bool:
        if left == right:
            return True
        return len(left) >= 4 and len(right) >= 4 and left[:4] == right[:4]

    @staticmethod
    def _match_location(locations, name: str):
        needle = name.casefold()
        for location in locations:
            if location.canonical_name.casefold() == needle:
                return location
            if any(alias.casefold() == needle for alias in location.aliases):
                return location
        return None


__all__ = ["DestinationAuthorization", "PlayerDestinationAuthorizer"]
