from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.entity_repo import EntityRepository
from app.db.tables import Character, Turn
from app.models.turn_authority import ExistingNpcArrival
from app.services.entity_identity import identity_key, resolve_character_candidates
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

    SYNTHETIC_PLACEHOLDERS = frozenset(
        {
            "безымянный собеседник",
            "неизвестный собеседник",
            "безымянный npc",
            "неизвестный npc",
            "unnamed interlocutor",
            "unknown interlocutor",
            "unnamed npc",
            "unknown npc",
        }
    )

    def __init__(self, session: AsyncSession):
        self._session = session
        self._entities = EntityRepository(session)

    @classmethod
    def sanitize_introductions(cls, introductions: list) -> list:
        """Keep temporary planner identities readable without canonizing fake placeholders.

        Planner control models occasionally leak CJK names on a Russian surface. Older code replaced
        those with the literal identity ``Безымянный собеседник``, which then became durable canon.
        At the authority boundary we instead derive a role identity (``Диспетчер``) when possible;
        if even the role is unusable, fail closed and let the planner repair the contact.
        """
        used: set[str] = set()
        result = []
        placeholder_keys = {identity_key(value) for value in cls.SYNTHETIC_PLACEHOLDERS}

        for introduction in introductions:
            canonical = " ".join(str(introduction.canonical_name or "").split())
            canonical_key = identity_key(canonical)
            needs_repair = contains_cjk(canonical) or canonical_key in placeholder_keys

            if needs_repair:
                role = " ".join(str(introduction.role or "").split())
                if not role or contains_cjk(role) or identity_key(role) in placeholder_keys:
                    raise AuthorityResolutionError(
                        "Planner returned an unreadable temporary NPC identity without a usable role"
                    )
                base = role[0].upper() + role[1:] if role else role
                candidate = base
                index = 2
                while identity_key(candidate) in used:
                    candidate = f"{base} {index}"
                    index += 1
                introduction = introduction.model_copy(
                    update={
                        "canonical_name": candidate,
                        "temporary_name": True,
                    }
                )
                canonical_key = identity_key(candidate)

            if canonical_key in used:
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
    ) -> NpcIntroductionResolution:
        introductions = self.sanitize_introductions(introductions)
        names = list(present_names)
        present_keys = {identity_key(value) for value in names}
        all_characters = await self._entities.list_by_campaign(
            campaign_id,
            entity_type="character",
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
        character_locations: dict[UUID, UUID | None] = {
            entity_id: (
                UUID(row.current_location_id) if row.current_location_id else None
            )
            for entity_id, row in character_states.items()
        }

        new_introductions = []
        existing_arrivals: list[ExistingNpcArrival] = []
        for introduction in introductions:
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
                new_introductions.append(introduction)
                continue

            existing_id, existing = next(iter(unique_matches.items()))
            existing_key = identity_key(existing.canonical_name)
            if existing_key in present_keys:
                continue

            character = character_states.get(existing_id)
            current_location_id = (
                UUID(character.current_location_id)
                if character and character.current_location_id
                else None
            )
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