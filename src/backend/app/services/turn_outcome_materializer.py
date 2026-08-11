from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.tables import Entity
from app.models.character import CharacterCreate
from app.models.turn_authority import TurnAuthority
from app.services.entity_identity import identity_key


@dataclass(frozen=True)
class MaterializedTurnOutcome:
    introduced_character_ids: tuple[UUID, ...] = ()
    arrived_existing_participants: tuple[tuple[UUID, UUID], ...] = ()

    @property
    def arrived_existing_character_ids(self) -> tuple[UUID, ...]:
        return tuple(entity_id for _scene_id, entity_id in self.arrived_existing_participants)

    @property
    def has_changes(self) -> bool:
        return bool(self.introduced_character_ids or self.arrived_existing_participants)


class TurnOutcomeMaterializer:
    """Apply structured outcomes before prose and bind their provenance after publication.

    New NPCs are created exactly once. Known NPC references normalized by TurnAuthority are attached
    to the target scene without recreating the entity, and only when Authority has already verified
    that the character is physically at the target location.
    """

    def __init__(self, session: AsyncSession):
        self._session = session
        self._entities = EntityRepository(session)
        self._scenes = SceneRepository(session)

    async def materialize(
        self,
        authority: TurnAuthority,
        *,
        source_turn_id: UUID,
    ) -> MaterializedTurnOutcome:
        if not authority.allowed_new_npcs and not authority.allowed_existing_npc_arrivals:
            return MaterializedTurnOutcome()
        if not authority.target_scene_id:
            raise ValueError("Planned NPC materialization has no authoritative target scene")

        existing_participants = set(
            await self._scenes.get_participants(authority.target_scene_id)
        )
        arrived_existing: list[tuple[UUID, UUID]] = []
        for arrival in authority.allowed_existing_npc_arrivals:
            if arrival.entity_id in existing_participants:
                continue
            # Authority already checked current_location_id == target location. Keep movement
            # disabled here so materialization can never turn an identity repair into teleportation.
            await self._scenes.add_participant(
                authority.target_scene_id,
                arrival.entity_id,
                allow_movement=False,
            )
            arrived_existing.append((authority.target_scene_id, arrival.entity_id))
            existing_participants.add(arrival.entity_id)

        known = await self._entities.list_by_campaign(
            authority.campaign_id,
            entity_type="character",
        )
        known_names: set[str] = set()
        for entity in known:
            known_names.add(identity_key(entity.canonical_name))
            known_names.update(identity_key(alias) for alias in entity.aliases)

        created_ids: list[UUID] = []
        for introduction in authority.allowed_new_npcs:
            key = identity_key(introduction.canonical_name)
            if key in known_names:
                raise ValueError(
                    "Cannot materialize planned new NPC because that identity already exists: "
                    f"{introduction.canonical_name}"
                )
            character = await self._entities.create_character(
                authority.campaign_id,
                CharacterCreate(
                    canonical_name=introduction.canonical_name,
                    description=introduction.description or introduction.role,
                    appearance=introduction.appearance,
                    voice=introduction.voice,
                    custom_fields={
                        "introduced_by": "turn_authority",
                        "introduction_turn_id": str(source_turn_id),
                        "introduction_trigger_turn_id": str(authority.trigger_turn_id),
                        "introduction_reason": introduction.reason,
                        "role": introduction.role,
                        "temporary_name": introduction.temporary_name,
                    },
                ),
            )
            await self._scenes.add_participant(
                authority.target_scene_id,
                character.id,
                allow_movement=True,
            )
            created_ids.append(character.id)
            known_names.add(key)

        await self._session.flush()
        return MaterializedTurnOutcome(
            introduced_character_ids=tuple(created_ids),
            arrived_existing_participants=tuple(arrived_existing),
        )

    async def bind_to_assistant(
        self,
        outcome: MaterializedTurnOutcome,
        assistant_turn_id: UUID,
    ) -> None:
        """Replace prepared user-turn provenance with the durable assistant source turn."""
        for entity_id in outcome.introduced_character_ids:
            row = await self._session.get(Entity, str(entity_id))
            if not row:
                continue
            try:
                fields = json.loads(row.custom_fields or "{}")
            except (json.JSONDecodeError, TypeError):
                fields = {}
            if not isinstance(fields, dict):
                fields = {}
            fields["introduction_turn_id"] = str(assistant_turn_id)
            row.custom_fields = json.dumps(fields, ensure_ascii=False)
        await self._session.flush()

    async def rollback(self, outcome: MaterializedTurnOutcome) -> None:
        """Compensate prepared entity changes when a real generation/state failure aborts."""
        for scene_id, entity_id in outcome.arrived_existing_participants:
            # Existing characters must never be deleted. Remove only the participant relation
            # inserted by this turn; historical participation in older scenes stays intact.
            await self._scenes.remove_participant(scene_id, entity_id)
        for entity_id in outcome.introduced_character_ids:
            await self._entities.delete(entity_id)
        await self._session.flush()


__all__ = ["MaterializedTurnOutcome", "TurnOutcomeMaterializer"]
