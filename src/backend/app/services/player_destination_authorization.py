from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.location_repo import LocationRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.scene_transition_table import SceneTransition
from app.db.tables import Turn
from app.services.scene_state_service import SceneStateService


@dataclass(frozen=True)
class DestinationAuthorization:
    applicable: bool
    authorized: bool
    reason: str
    destination: str
    matched_clause: str | None = None
    destination_exists: bool = False


@dataclass(frozen=True)
class _InputClause:
    text: str
    travel: bool


class PlayerDestinationAuthorizer:
    """Independently authorize planner destinations from persisted player input.

    Human intent and the structural location graph are both authority. A return/back reference may
    use one unique direct route or one uniquely matching previously visited physical location before
    falling back to campaign-global text matching. Ordinary generic travel remains fail-closed when
    multiple campaign locations match. References such as "the address you named" are resolved only
    against recently published assistant text.
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
    RETURN_TRAVEL_RE = re.compile(
        r"\b(?:return|returning|back|возвраща\w*|обратно)\b",
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
    EXIT_TRAVEL_RE = re.compile(
        r"\b(?:(?:иду|пойду|выхожу|выходим|направляюсь)\s+"
        r"(?:к\s+выходу|наружу|на\s+улицу)|к\s+выходу|выхожу\s+наружу)\b",
        re.IGNORECASE,
    )
    OUTWARD_INTENT_RE = re.compile(
        r"\b(?:наружу|на\s+улицу|к\s+выходу)\b",
        re.IGNORECASE,
    )
    RETURN_EXIT_LABEL_RE = re.compile(
        r"\b(?:обратно|назад|внутрь|return|back)\b",
        re.IGNORECASE,
    )
    KNOWN_REFERENCE_NOUN_RE = re.compile(
        r"\b(?:address|place|location|destination|адрес\w*|мест\w*|локац\w*|точк\w*)\b",
        re.IGNORECASE,
    )
    KNOWN_REFERENCE_ATTRIBUTION_RE = re.compile(
        r"\b(?:named|mentioned|gave|said|told|provided|назва\w*|назван\w*|указа\w*|"
        r"указан\w*|сказа\w*|говор\w*|сообщ\w*)\b",
        re.IGNORECASE,
    )
    GENERIC_LOCATION_TOKENS = frozenset(
        {
            "бар", "дом", "здание", "квартира", "комната", "офис", "улица",
            "район", "квартал", "департамент", "вход", "подвал", "тоннель", "зал",
            "рынок", "door", "room", "street", "district", "office", "apartment",
            "building", "department", "entrance", "basement", "tunnel", "hall",
            "quarter", "market", "bar",
        }
    )
    DESTINATION_STOP_TOKENS = frozenset(
        {
            "около", "возле", "рядом", "near", "around", "the", "and", "with",
            "который", "которая", "которое",
        }
    )

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
        clean_destination = " ".join((destination or "").split())
        if not trigger_turn_id or not clean_destination:
            return self._unresolved(clean_destination, "no human trigger or destination")

        turn = await self._session.get(Turn, str(trigger_turn_id))
        if not turn or turn.role != "user":
            return self._unresolved(clean_destination, "trigger is not a human user turn")

        locations = await self._locations.list_by_campaign(UUID(turn.campaign_id))
        source_location_id = await self._source_location_id(turn)
        source_location = next((item for item in locations if item.id == source_location_id), None)
        clean_destination = self._strip_source_suffix(
            clean_destination,
            source_location.canonical_name if source_location else None,
        )
        target = self._match_location(locations, clean_destination)
        target_exists = target is not None
        input_text = turn.content or ""
        clauses = self._clauses(input_text)
        published_reference = self._is_published_reference(input_text)
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
                return self._authorized(
                    clean_destination,
                    "destination is specifically named in a travel clause",
                    clause.text,
                    target_exists,
                )
            if generic_matches:
                # Return semantics can use physical history before global name matching. A reverse
                # edge resolves the immediate previous place; older visited locations remain valid
                # return targets without inventing a fake direct edge in the location graph.
                if self.RETURN_TRAVEL_RE.search(clause.text):
                    direct = await self._compatible_direct_routes(
                        UUID(turn.campaign_id),
                        source_location_id,
                        locations,
                        generic_matches,
                    )
                    if len(direct) == 1 and target and direct[0].id == target.id:
                        return self._authorized(
                            clean_destination,
                            "generic return reference resolves to one direct structural route",
                            clause.text,
                            True,
                        )
                    if len(direct) > 1:
                        ambiguous_generic = True

                    visited = await self._compatible_visited_locations(
                        UUID(turn.campaign_id),
                        source_location_id,
                        locations,
                        generic_matches,
                        before=turn.created_at,
                    )
                    if len(visited) == 1 and target and visited[0].id == target.id:
                        return self._authorized(
                            clean_destination,
                            "generic return reference resolves to one previously visited physical location",
                            clause.text,
                            True,
                        )
                    if len(visited) > 1:
                        ambiguous_generic = True

                compatible = self._compatible_locations(locations, generic_matches)
                if len(compatible) == 1 and target and compatible[0].id == target.id:
                    return self._authorized(
                        clean_destination,
                        "generic travel reference resolves to one known location",
                        clause.text,
                        True,
                    )
                if len(compatible) > 1:
                    ambiguous_generic = True

            if self.ANAPHORIC_TRAVEL_RE.search(clause.text):
                anaphoric_travel = True

        # Relative clauses are deliberately allowed to span clause-parser boundaries. For example,
        # "еду по адресу, который вы назвали" is split at the comma, but the human commitment and
        # attribution belong to one input. It still authorizes only a destination grounded by at
        # least two specific tokens in previously published active assistant text.
        if published_reference and any(clause.travel for clause in clauses):
            if await self._recently_published_destination(turn, clean_destination):
                return self._authorized(
                    clean_destination,
                    "anaphoric travel resolves to a recently published destination",
                    input_text,
                    target_exists,
                )

        if ambiguous_generic:
            return DestinationAuthorization(
                applicable=True,
                authorized=False,
                reason="player destination reference is ambiguous",
                destination=clean_destination,
                destination_exists=target_exists,
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
                    destination_exists=target_exists,
                )
            if anaphoric_travel:
                if specific_match:
                    return self._authorized(
                        clean_destination,
                        "anaphoric travel resolves to a committed destination",
                        clause.text,
                        target_exists,
                    )
                compatible = self._compatible_locations(locations, generic_matches)
                if len(compatible) == 1 and target and compatible[0].id == target.id:
                    return self._authorized(
                        clean_destination,
                        "anaphoric travel resolves to one committed destination",
                        clause.text,
                        True,
                    )
                if len(compatible) > 1:
                    return DestinationAuthorization(
                        applicable=True,
                        authorized=False,
                        reason="anaphoric destination reference is ambiguous",
                        destination=clean_destination,
                        matched_clause=clause.text,
                        destination_exists=target_exists,
                    )
            return self._unresolved(
                clean_destination,
                "destination is implied by a committed non-travel action",
                clause.text,
                destination_exists=target_exists,
            )

        if self.EXIT_TRAVEL_RE.search(input_text):
            unique = await self._unique_available_exit(turn)
            if unique is not None:
                unique_exit, exit_row = unique
                if self.OUTWARD_INTENT_RE.search(input_text) and self._is_return_exit(
                    exit_row
                ):
                    unique_exit = None
                dest_fold = clean_destination.casefold()
                dest_is_exit_word = bool(
                    re.search(r"\b(?:выход\w*|наружу|улиц\w*|двер\w*)\b", dest_fold)
                )
                same_place = bool(
                    target is not None
                    and source_location_id is not None
                    and target.id == source_location_id
                )
                named_elsewhere = bool(
                    target_exists
                    and target is not None
                    and source_location_id is not None
                    and target.id != source_location_id
                    and not dest_is_exit_word
                )
                if unique_exit is not None and not named_elsewhere and (
                    dest_is_exit_word or same_place or not target_exists
                ):
                    return self._authorized(
                        unique_exit.canonical_name,
                        "unique available exit from travel clause",
                        input_text,
                        True,
                    )

        if self.RETURN_TRAVEL_RE.search(input_text):
            unique = await self._unique_available_exit(turn)
            if unique is not None:
                unique_exit, _exit_row = unique
                return self._authorized(
                    unique_exit.canonical_name,
                    "unique available exit from return clause",
                    input_text,
                    True,
                )

        if anaphoric_travel:
            return self._unresolved(
                clean_destination,
                "travel clause uses an unresolved anaphoric destination",
                destination_exists=target_exists,
            )
        return self._unresolved(
            clean_destination,
            "player input does not independently identify planner destination",
            destination_exists=target_exists,
        )

    @classmethod
    def _is_return_exit(cls, exit_row) -> bool:
        label = " ".join(str(getattr(exit_row, "label", "") or "").split())
        direction = " ".join(str(getattr(exit_row, "direction", "") or "").split())
        return bool(
            cls.RETURN_EXIT_LABEL_RE.search(label)
            or cls.RETURN_EXIT_LABEL_RE.search(direction)
        )

    async def _unique_available_exit(self, turn: Turn):
        source_id = await self._source_location_id(turn)
        if source_id is None:
            return None
        campaign_id = UUID(turn.campaign_id)
        exits = await self._state.list_exits(
            campaign_id,
            source_id,
            include_hidden=False,
        )
        outward = [
            row
            for row in exits
            if row.to_location_id and row.to_location_id != source_id
        ]
        targets = {row.to_location_id for row in outward}
        if len(targets) != 1:
            return None
        target_id = next(iter(targets))
        locations = await self._locations.list_by_campaign(campaign_id)
        location = next((item for item in locations if item.id == target_id), None)
        if location is None:
            return None
        exit_row = next(row for row in outward if row.to_location_id == target_id)
        return location, exit_row

    async def _source_location_id(self, turn: Turn) -> UUID | None:
        if not turn.scene_id:
            return None
        try:
            return await self._scenes.get_location_id(UUID(str(turn.scene_id)))
        except (TypeError, ValueError):
            return None

    async def _compatible_direct_routes(
        self,
        campaign_id: UUID,
        source_location_id: UUID | None,
        locations,
        generic_matches: set[str],
    ):
        if source_location_id is None or not generic_matches:
            return []
        exits = await self._state.list_exits(
            campaign_id,
            source_location_id,
            include_hidden=True,
        )
        target_ids = {row.to_location_id for row in exits}
        return [
            location
            for location in locations
            if location.id in target_ids
            and self._location_matches_generic(location.canonical_name, generic_matches)
        ]

    async def _compatible_visited_locations(
        self,
        campaign_id: UUID,
        source_location_id: UUID | None,
        locations,
        generic_matches: set[str],
        *,
        before,
    ):
        """Return generic-matching locations proven to have been physically visited before this turn."""
        if not generic_matches:
            return []
        rows = (
            await self._session.execute(
                select(SceneTransition)
                .where(
                    SceneTransition.campaign_id == str(campaign_id),
                    SceneTransition.transition_type == "location_transition",
                    SceneTransition.status.in_(("prepared", "applied")),
                    SceneTransition.undone_at.is_(None),
                    SceneTransition.created_at <= before,
                )
                .order_by(SceneTransition.created_at.desc())
            )
        ).scalars().all()

        visited_ids: list[UUID] = []
        seen: set[UUID] = set()
        for row in rows:
            for raw in (row.target_location_id, row.source_location_id):
                if not raw:
                    continue
                try:
                    location_id = UUID(str(raw))
                except (TypeError, ValueError):
                    continue
                if location_id == source_location_id or location_id in seen:
                    continue
                seen.add(location_id)
                visited_ids.append(location_id)

        by_id = {location.id: location for location in locations}
        return [
            by_id[location_id]
            for location_id in visited_ids
            if location_id in by_id
            and self._location_matches_generic(
                by_id[location_id].canonical_name,
                generic_matches,
            )
        ]

    async def _recently_published_destination(self, turn: Turn, destination: str) -> bool:
        specific = self._specific_destination_tokens(destination)
        if len(specific) < 2:
            return False
        rows = (
            await self._session.execute(
                select(Turn)
                .where(
                    Turn.campaign_id == turn.campaign_id,
                    Turn.role == "assistant",
                    Turn.status == "active",
                    Turn.created_at <= turn.created_at,
                )
                .order_by(Turn.created_at.desc())
                .limit(12)
            )
        ).scalars().all()
        for row in rows:
            tokens = self.TOKEN_RE.findall((row.content or "").casefold())
            matched = {
                target
                for target in specific
                if any(self._tokens_match(target, source) for source in tokens)
            }
            if len(matched) >= 2:
                return True
        return False

    @classmethod
    def _is_published_reference(cls, text: str) -> bool:
        return bool(
            cls.KNOWN_REFERENCE_NOUN_RE.search(text)
            and cls.KNOWN_REFERENCE_ATTRIBUTION_RE.search(text)
        )

    @classmethod
    def _specific_destination_tokens(cls, destination: str) -> set[str]:
        return {
            token
            for token in cls.TOKEN_RE.findall(destination.casefold())
            if len(token) >= 4
            and token not in cls.GENERIC_LOCATION_TOKENS
            and token not in cls.DESTINATION_STOP_TOKENS
        }

    @staticmethod
    def _strip_source_suffix(destination: str, source_name: str | None) -> str:
        if not source_name:
            return destination
        folded = destination.casefold()
        source_folded = source_name.casefold()
        for separator in (" — ", " - ", " > ", " / "):
            suffix = f"{separator}{source_folded}"
            if folded.endswith(suffix):
                prefix = destination[: -len(suffix)].strip(" ,—->/")
                if prefix:
                    return prefix
        return destination

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

    @classmethod
    def _compatible_locations(cls, locations, generic_matches: set[str]):
        return [
            location
            for location in locations
            if cls._location_matches_generic(location.canonical_name, generic_matches)
        ]

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
