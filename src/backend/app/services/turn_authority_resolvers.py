from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.location_repo import LocationRepository
from app.db.tables import Character, Turn
from app.models.turn_authority import ExistingNpcArrival, PlannedNpcIntroduction
from app.services.entity_identity import exact_identity_matches, identity_key, resolve_character_candidates
from app.services.name_identity_contract import (
    description_used_as_identity_name,
    is_usable_short_designation,
    repair_introduction_identity,
)
from app.services.name_identity_contract import (
    accept_short_canonical,
    allocate_needs_name_canonical,
    designation_locale_mismatch,
)
from app.services.player_intent_contract import contains_cjk


class AuthorityResolutionError(ValueError):
    pass


class ActorResolver:
    """Resolve the explicitly selected actor from input-routing provenance."""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def resolve_id(
        self,
        trigger_turn_id: UUID,
        explicit_actor_id: UUID | None,
    ) -> UUID | None:
        if explicit_actor_id is not None:
            return explicit_actor_id
        row = await self._session.get(Turn, str(trigger_turn_id))
        if not row or not row.context_snapshot:
            return None
        try:
            snapshot = json.loads(row.context_snapshot)
        except (TypeError, json.JSONDecodeError):
            return None
        routing = snapshot.get("input_routing") if isinstance(snapshot, dict) else None
        value = routing.get("addressed_character_id") if isinstance(routing, dict) else None
        try:
            return UUID(str(value)) if value else None
        except (TypeError, ValueError):
            return None


@dataclass(frozen=True)
class NpcIntroductionResolution:
    new_introductions: list
    existing_arrivals: list[ExistingNpcArrival]
    present_names: list[str]


class NpcIntroductionResolver:
    """Classify planned NPC introductions against structured identity and presence state."""

    def __init__(self, session: AsyncSession):
        self._session = session
        self._entities = EntityRepository(session)

    @classmethod
    def sanitize_introductions(
        cls,
        introductions: list,
        *,
        occupied_canonical_keys: set[str] | frozenset[str] | None = None,
        locale_text: str | None = None,
    ) -> list:
        """Keep planner identities readable without canonizing unsupported personal names.

        Planner control models occasionally leak CJK/synthetic names or claim a stable personal
        identity without current/campaign evidence. At the final authority boundary we derive the
        grounded role identity and keep it temporary. If no usable role exists, fail closed rather
        than persisting an invented stable name. Short designations that collide with occupied
        campaign/batch keys fail soft to needs_name status + human failsoft via the shared contract.

        ``locale_text`` (typically the player input) feeds the existing
        ``designation_locale_mismatch`` contract so a Russian turn cannot materialize a
        Latin-script-only twin like ``Housekeeper`` beside ``Управляющая домом`` — not a
        job-title word list, the same orthographic invent-people gate.
        """
        used: set[str] = set(occupied_canonical_keys or ())
        result = []
        locale_hint = " ".join(str(locale_text or "").split())

        for introduction in introductions:
            # Reject description-as-name / long role-blurb identities before other repairs.
            try:
                introduction = repair_introduction_identity(
                    introduction,
                    occupied_canonical_keys=used,
                    locale_text=locale_hint or None,
                )
            except ValueError as exc:
                raise AuthorityResolutionError(str(exc)) from exc

            canonical = " ".join(str(introduction.canonical_name or "").split())
            canonical_key = identity_key(canonical)
            evidence = " ".join(str(introduction.personal_name_evidence or "").split())
            role = " ".join(str(introduction.role or "").split())
            usable_role = bool(
                is_usable_short_designation(role)
                and not contains_cjk(role)
            )
            unsupported_stable_name = not introduction.temporary_name and not evidence
            # A temporary flag is not evidence. An invented personal label must still collapse
            # to the grounded role before publication, or identity binding fail-closes the turn.
            unproven_personal_label = not evidence and usable_role
            description_as_name = description_used_as_identity_name(
                canonical,
                role=role,
                description=getattr(introduction, "description", None),
            )
            locale_for_check = " ".join(
                part
                for part in (
                    locale_hint,
                    getattr(introduction, "description", None) or "",
                    role,
                    canonical,
                )
                if part
            )
            latin_only_against_russian = bool(
                locale_hint
                and designation_locale_mismatch(canonical, locale_text=locale_hint)
            )
            needs_repair = (
                contains_cjk(canonical)
                or unsupported_stable_name
                or unproven_personal_label
                or description_as_name
                or not is_usable_short_designation(canonical)
                or latin_only_against_russian
            )

            if needs_repair:
                if not usable_role or (
                    latin_only_against_russian
                    and designation_locale_mismatch(role, locale_text=locale_hint)
                ):
                    # Latin-only role under a Russian player turn has no grounded local role.
                    if latin_only_against_russian:
                        raise AuthorityResolutionError(
                            "Planner returned a Latin-script-only NPC identity on a Russian turn "
                            "without a usable grounded role"
                        )
                    raise AuthorityResolutionError(
                        "Planner returned an unsupported NPC identity without a usable grounded role"
                    )
                accepted = accept_short_canonical(
                    role,
                    occupied_canonical_keys=used,
                    locale_text=locale_for_check,
                    allow_locale_mismatch=False,
                )
                candidate = accepted or allocate_needs_name_canonical(
                    used,
                    role=role,
                    description=getattr(introduction, "description", None),
                    previous=canonical,
                )
                if not candidate:
                    raise AuthorityResolutionError(
                        "Planner returned an unsupported NPC identity without a usable grounded role"
                    )
                introduction = introduction.model_copy(
                    update={
                        "canonical_name": candidate,
                        "temporary_name": True,
                        "personal_name_evidence": None,
                    }
                )
                canonical_key = identity_key(candidate)

            if canonical_key in used:
                # Shared contract already preferred needs_name; remaining duplicates fail closed.
                raise AuthorityResolutionError(
                    f"Planner returned duplicate NPC identity: {introduction.canonical_name}"
                )
            used.add(canonical_key)
            result.append(introduction)
        return result

    async def resolve(
        self,
        *,
        campaign_id: UUID,
        introductions: list,
        present_names: list[str],
        target_location_id: UUID | None,
        bringing_steps: frozenset[int] = frozenset(),
    ) -> NpcIntroductionResolution:
        references = [
            getattr(item, "identity_reference", None) or item.canonical_name
            for item in introductions
        ]
        names = list(present_names)
        present_keys = {identity_key(value) for value in names}
        all_characters = await self._entities.list_by_campaign(
            campaign_id,
            entity_type="character",
        )
        place = await LocationRepository(self._session).get_by_id(target_location_id) if target_location_id else None
        role = ((place.custom_fields or {}).get("resident_role") if place else None) or ""
        slot = str(target_location_id)
        filled = any((entity.custom_fields or {}).get("slot_id") == slot for entity in all_characters)
        if role:
            # Someone found at a keeper's place (not brought by a step) is its keeper: they fill an
            # empty slot or are the keeper already there (B8 T6: «Хозяин…» beside «Трактирщик»).
            found = [item for item in introductions if not getattr(item, "resident_slot", None)
                     and getattr(item, "after_action_index", None) not in bringing_steps]
            keepers = found if filled else found[:1]
            introductions = [item.model_copy(update={"resident_slot": slot})
                             if any(item is keeper for keeper in keepers) else item for item in introductions]
        if role and not filled \
                and not any(getattr(item, "resident_slot", None) == slot for item in introductions):
            # The keeper of a typed resident slot is at their place: authorized the moment the
            # player is there (B6 T3: «трактирщик у стойки» on arrival was an absent character).
            keeper = PlannedNpcIntroduction(canonical_name=role[:1].upper() + role[1:], role=role,
                                            temporary_name=True, resident_slot=slot,
                                            reason="Хранитель этого места находится на месте.")
            introductions, references = [*introductions, keeper], [*references, keeper.canonical_name]
        reserved_for_sanitize = {
            identity_key(value)
            for entity in all_characters
            for value in (entity.canonical_name, *entity.aliases)
        }
        introductions = self.sanitize_introductions(
            introductions,
            occupied_canonical_keys=reserved_for_sanitize,
        )

        ids = [str(entity.id) for entity in all_characters]
        rows = []
        if ids:
            rows = (
                await self._session.execute(
                    select(Character).where(Character.entity_id.in_(ids))
                )
            ).scalars().all()
        character_states = {UUID(row.entity_id): row for row in rows}
        # The target place with its parent and child places is one establishment for identity
        # (the inn's cook is the kitchen's cook). Typed location IDs only.
        same_place = {target_location_id} if target_location_id else set()
        if target_location_id:
            for location in await LocationRepository(self._session).list_by_campaign(campaign_id):
                if location.id == target_location_id and location.parent_location_id:
                    same_place.add(location.parent_location_id)
                if location.parent_location_id == target_location_id:
                    same_place.add(location.id)
        character_locations: dict[UUID, UUID | None] = {
            entity_id: (
                target_location_id if location in same_place
                else location
            )
            for entity_id, row in character_states.items()
            for location in [UUID(row.current_location_id) if row.current_location_id else None]
        }

        new_introductions = []
        reserved_names = {
            identity_key(value)
            for entity in all_characters
            for value in (entity.canonical_name, *entity.aliases)
        }
        existing_arrivals: list[ExistingNpcArrival] = []
        holders = {
            (entity.custom_fields or {}).get("slot_id"): entity for entity in all_characters
        }
        for introduction, reference in zip(introductions, references):
            holder = holders.get(introduction.resident_slot) if introduction.resident_slot else None
            if holder is not None and character_locations.get(UUID(str(holder.id))) != target_location_id:
                continue  # The slot's keeper is elsewhere: nobody new takes the slot (ban 1).
            # A resident slot has one keeper; otherwise role normalization must not erase an
            # existing identity reference: temporary designations local, stable names global.
            matches = [holder] if holder is not None else [
                entity for entity in exact_identity_matches(all_characters, reference)
                if not (entity.custom_fields or {}).get("temporary_name")
                or (
                    target_location_id is not None
                    and character_locations.get(entity.id) == target_location_id
                )
            ]
            if not matches and holder is None:
                matches = resolve_character_candidates(
                    all_characters,
                    proposed_name=introduction.canonical_name,
                    proposed_role=introduction.role,
                    temporary_name=introduction.temporary_name,
                    target_location_id=target_location_id,
                    character_locations=character_locations,
                )
            unique_matches = {UUID(str(entity.id)): entity for entity in matches}
            if len(unique_matches) > 1:
                candidate_names = ", ".join(
                    sorted(entity.canonical_name for entity in unique_matches.values())
                )
                raise AuthorityResolutionError(
                    "Planner NPC identity is ambiguous for "
                    f"{introduction.canonical_name}: {candidate_names}"
                )
            if not unique_matches:
                # Campaign-unique labels via shared contract: never twin another live
                # canonical_name; collide → needs_name status + human failsoft label.
                base = introduction.canonical_name
                role_text = getattr(introduction, "role", None)
                desc_text = getattr(introduction, "description", None)
                accepted = accept_short_canonical(
                    base,
                    occupied_canonical_keys=reserved_names,
                    locale_text=" ".join(
                        part
                        for part in (
                            desc_text or "",
                            role_text or "",
                            base,
                        )
                        if part
                    ),
                    allow_locale_mismatch=bool(
                        getattr(introduction, "personal_name_evidence", None)
                    ),
                    personal=not introduction.temporary_name,
                )
                candidate = accepted or allocate_needs_name_canonical(
                    reserved_names,
                    role=role_text,
                    description=desc_text,
                    previous=base,
                )
                if not candidate:
                    raise AuthorityResolutionError(
                        "Planner NPC identity collided without a usable fail-soft designation"
                    )
                introduction = introduction.model_copy(
                    update={
                        "canonical_name": candidate,
                        "temporary_name": True
                        if accepted is None
                        else introduction.temporary_name,
                    }
                )
                reserved_names.add(identity_key(candidate))
                new_introductions.append(introduction)
                continue

            existing_id, existing = next(iter(unique_matches.items()))
            existing_key = identity_key(existing.canonical_name)
            if existing_key in present_keys:
                continue

            current_location_id = character_locations.get(existing_id)
            if target_location_id and current_location_id == target_location_id:
                existing_arrivals.append(
                    ExistingNpcArrival(
                        entity_id=existing_id,
                        canonical_name=existing.canonical_name,
                        reason=introduction.reason,
                    )
                )
                names.append(existing.canonical_name)
                present_keys.add(existing_key)
                continue

            location = str(current_location_id) if current_location_id else "неизвестна"
            target = str(target_location_id) if target_location_id else "неизвестна"
            raise AuthorityResolutionError(
                "Известный персонаж не может появиться без структурного перемещения: "
                f"{existing.canonical_name} находится в {location}, target location = {target}"
            )

        # Authorized first appearances are physically present for narrator/validator this turn.
        for introduction in new_introductions:
            key = identity_key(introduction.canonical_name)
            if key and key not in present_keys:
                names.append(introduction.canonical_name)
                present_keys.add(key)

        return NpcIntroductionResolution(
            new_introductions=new_introductions,
            existing_arrivals=existing_arrivals,
            present_names=names,
        )


__all__ = [
    "ActorResolver",
    "AuthorityResolutionError",
    "NpcIntroductionResolution",
    "NpcIntroductionResolver",
]
