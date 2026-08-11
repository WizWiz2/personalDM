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


@dataclass(frozen=True)
class _InputClause:
    text: str
    travel: bool


class PlayerDestinationAuthorizer:
    """Independently authorize planner destinations from persisted player input.

    The result is deliberately tri-state through ``applicable``/``authorized``:

    * applicable + authorized: the player clearly selected this destination;
    * applicable + unauthorized: the input clearly does not authorize this choice;
    * not applicable: the text is inconclusive, so only an already-existing
      structural route may carry the movement. Inconclusive text never grants
      route discovery or creation of a new destination.
    """

    TOKEN_RE = re.compile(r"[a-zа-яё0-9]+", re.IGNORECASE)
    TRAVEL_ANCHOR_RE = re.compile(
        r"\b(?:"
        r"go|going|return|returning|enter|entering|leave|leaving|head|heading|"
        r"travel|traveling|travelling|drive|driving|ride|riding|get|getting|"
        r"walk|walking|step|stepping|make\s+(?:my|our|their|his|her)\s+way|"
        r"иду|пойду|еду|поеду|отправляюсь|направляюсь|возвращаюсь|вхожу|захожу|"
        r"зайду|выхожу|ухожу|уйду|выезжаю|добираюсь|доберусь|следую|"
        r"спускаюсь|поднимаюсь"
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
    NONCOMMITTAL_RE = re.compile(
        r"\b(?:wonder|wondering|what(?:'s|\s+is)?|whether|maybe|perhaps|consider|"
        r"curious|думаю|интересно|может|возможно|ли|что\s+там|что\s+в)\b",
        re.IGNORECASE,
    )
    ANAPHORIC_TRAVEL_RE = re.compile(
        r"\b(?:there|inside|outside|back|туда|сюда|там|обратно|внутрь|наружу|домой)\b",
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
            return self._unresolved(clean_destination, "no human trigger or destination")

        turn = await self._session.get(Turn, str(trigger_turn_id))
        if not turn or turn.role != "user":
            return self._unresolved(
                clean_destination,
                "trigger is not a human user turn",
            )

        clauses = self._clauses(turn.content or "")
        locations = await self._locations.list_by_campaign(UUID(turn.campaign_id))
        target = self._match_location(locations, clean_destination)
        ambiguous_generic = False
        anaphoric_travel = False

        for clause in clauses:
            if not clause.travel:
                continue
            specific_match, generic_matches = self._destination_reference(
                clause.text,
                clean_destination,
            )
            if specific_match:
                return DestinationAuthorization(
                    applicable=True,
                    authorized=True,
                    reason="destination is specifically named in a travel clause",
                    destination=clean_destination,
                    matched_clause=clause.text,
                )
            if generic_matches:
                compatible = [
                    location
                    for location in locations
                    if self._location_matches_generic(
                        location.canonical_name,
                        generic_matches,
                    )
                ]
                if len(compatible) == 1 and target and compatible[0].id == target.id:
                    return DestinationAuthorization(
                        applicable=True,
                        authorized=True,
                        reason="generic travel reference resolves to one known location",
                        destination=clean_destination,
                        matched_clause=clause.text,
                    )
                if len(compatible) > 1:
                    ambiguous_generic = True
            if self.ANAPHORIC_TRAVEL_RE.search(clause.text):
                anaphoric_travel = True

        if ambiguous_generic:
            return DestinationAuthorization(
                applicable=True,
                authorized=False,
                reason="player destination reference is ambiguous",
                destination=clean_destination,
            )

        for clause in clauses:
            if clause.travel:
                continue
            specific_match, generic_matches = self._destination_reference(
                clause.text,
                clean_destination,
            )
            if not specific_match and not generic_matches:
                continue
            if self.NONCOMMITTAL_RE.search(clause.text):
                return DestinationAuthorization(
                    applicable=True,
                    authorized=False,
                    reason="destination is only mentioned in a non-committal clause",
                    destination=clean_destination,
                    matched_clause=clause.text,
                )
            return self._unresolved(
                clean_destination,
                "destination is implied by a committed non-travel action",
                clause.text,
            )

        if anaphoric_travel:
            return self._unresolved(
                clean_destination,
                "travel clause uses an anaphoric destination",
            )
        return self._unresolved(
            clean_destination,
            "player input does not independently identify planner destination",
        )

    @classmethod
    def _clauses(cls, text: str) -> list[_InputClause]:
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

        result: list[_InputClause] = []
        previous_was_travel = False
        for part in parts:
            explicit = bool(cls.TRAVEL_ANCHOR_RE.search(part))
            elliptical = previous_was_travel and bool(
                cls.ELLIPTICAL_TRAVEL_RE.search(part)
            )
            travel = explicit or elliptical
            result.append(_InputClause(text=part, travel=travel))
            previous_was_travel = travel
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

    @staticmethod
    def _unresolved(
        destination: str,
        reason: str,
        matched_clause: str | None = None,
    ) -> DestinationAuthorization:
        return DestinationAuthorization(
            applicable=False,
            authorized=False,
            reason=reason,
            destination=destination,
            matched_clause=matched_clause,
        )


__all__ = ["DestinationAuthorization", "PlayerDestinationAuthorizer"]
