from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import delete

from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.tables import Entity, SceneParticipant
from app.models.character import CharacterCreate
from app.models.turn_authority import TurnAuthority
from app.services.entity_identity import identity_key


@dataclass(frozen=True)
class IdentityUpdate:
    entity_id: UUID
    previous_name: str
    previous_aliases: str
    previous_custom_fields: str

    def snapshot(self) -> dict:
        return {
            "entity_id": str(self.entity_id), "previous_name": self.previous_name,
            "previous_aliases": self.previous_aliases,
            "previous_custom_fields": self.previous_custom_fields,
        }


@dataclass(frozen=True)
class MaterializedTurnOutcome:
    introduced_character_ids: tuple[UUID, ...] = ()
    arrived_existing_participants: tuple[tuple[UUID, UUID], ...] = ()
    identity_updates: tuple[IdentityUpdate, ...] = ()

    @property
    def arrived_existing_character_ids(self) -> tuple[UUID, ...]:
        return tuple(entity_id for _scene_id, entity_id in self.arrived_existing_participants)

    @property
    def has_changes(self) -> bool:
        return bool(
            self.introduced_character_ids or self.arrived_existing_participants or self.identity_updates
        )


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
        reveal_requested = bool(authority.addressed_response and authority.addressed_response.revealed_name)
        if not authority.allowed_new_npcs and not authority.allowed_existing_npc_arrivals and not reveal_requested:
            return MaterializedTurnOutcome()
        if not authority.target_scene_id:
            raise ValueError("Planned NPC materialization has no authoritative target scene")

        existing_participants = set(await self._scenes.get_participants(authority.target_scene_id))
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
        # Introductions and identity revelation are independent effects. Authority
        # resolves which actors are new; a name question cannot suppress another
        # authorized arrival based on how many temporary participants are present.
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
            response = authority.addressed_response
            if response and identity_key(response.speaker_name or "") == key:
                authority.addressed_response = response.model_copy(
                    update={"speaker_id": character.id}
                )
                authority.acting_character_id = character.id
                authority.acting_character_name = character.canonical_name
            if character.canonical_name not in authority.present_character_names:
                authority.present_character_names.append(character.canonical_name)
            known_names.add(key)

        await self._session.flush()
        identity_update = await self._reveal_identity(authority, source_turn_id)
        return MaterializedTurnOutcome(
            introduced_character_ids=tuple(created_ids),
            arrived_existing_participants=tuple(arrived_existing),
            identity_updates=(identity_update,) if identity_update else (),
        )

    async def _reveal_identity(self, authority, source_turn_id) -> IdentityUpdate | None:
        response = authority.addressed_response
        if not response or not response.revealed_name:
            return None
        if response.speaker_id is None:
            # A name with no bound speaker is not a rename. Skip it; do not kill the turn.
            return None
        row = await self._session.get(Entity, str(response.speaker_id))
        if not row or row.campaign_id != str(authority.campaign_id):
            raise ValueError("Name revelation speaker is outside the campaign")
        if identity_key(row.canonical_name) == identity_key(response.revealed_name):
            return None
        fields = json.loads(row.custom_fields or "{}")
        if not fields.get("temporary_name"):
            raise ValueError("Name revelation cannot overwrite an established personal identity")
        known = await self._entities.list_by_campaign(authority.campaign_id)
        if any(
            entity.id != response.speaker_id and identity_key(response.revealed_name) in {
                identity_key(entity.canonical_name), *(identity_key(alias) for alias in entity.aliases)
            } for entity in known
        ):
            raise ValueError("Name revelation conflicts with another existing identity")
        update = IdentityUpdate(
            response.speaker_id, row.canonical_name, row.aliases or "[]", row.custom_fields or "{}",
        )
        old_name = row.canonical_name
        aliases = json.loads(row.aliases or "[]")
        if old_name not in aliases:
            aliases.append(old_name)
        row.canonical_name = response.revealed_name
        row.aliases = json.dumps(aliases, ensure_ascii=False)
        fields.update(
            temporary_name=False, identity_promoted_from=old_name,
            identity_promoted_turn_id=str(source_turn_id),
        )
        row.custom_fields = json.dumps(fields, ensure_ascii=False)
        authority.present_character_names = [
            response.revealed_name if identity_key(name) == identity_key(old_name) else name
            for name in authority.present_character_names
        ]
        authority.allowed_new_npcs = [
            npc.model_copy(update={
                "canonical_name": response.revealed_name, "temporary_name": False,
                "personal_name_evidence": response.name_evidence,
            }) if identity_key(npc.canonical_name) == identity_key(old_name) else npc
            for npc in authority.allowed_new_npcs
        ]
        authority.addressed_response = response.model_copy(update={
            "speaker_name": response.revealed_name,
            "speaker_aliases": list(dict.fromkeys([*response.speaker_aliases, old_name])),
        })
        authority.addressed_response_obligation = response.revealed_name
        authority.acting_character_id = response.speaker_id
        authority.acting_character_name = response.revealed_name
        await self._session.flush()
        return update

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
        for update in outcome.identity_updates:
            row = await self._session.get(Entity, str(update.entity_id))
            if row:
                fields = json.loads(row.custom_fields or "{}")
                fields["identity_promoted_turn_id"] = str(assistant_turn_id)
                row.custom_fields = json.dumps(fields, ensure_ascii=False)
        await self._session.flush()

    async def rollback(self, outcome: MaterializedTurnOutcome) -> None:
        """Compensate prepared entity changes when a real generation/state failure aborts."""
        for update in outcome.identity_updates:
            row = await self._session.get(Entity, str(update.entity_id))
            if row:
                row.canonical_name = update.previous_name
                row.aliases = update.previous_aliases
                row.custom_fields = update.previous_custom_fields
        for scene_id, entity_id in outcome.arrived_existing_participants:
            # Existing characters must never be deleted. Remove only the participant relation
            # inserted by this turn; historical participation in older scenes stays intact.
            await self._scenes.remove_participant(scene_id, entity_id)
        for entity_id in outcome.introduced_character_ids:
            # SQLite deployments may not enable FK cascades. Remove the introduced relation
            # explicitly so compensation cannot leave a ghost participant pointing at no entity.
            await self._session.execute(
                delete(SceneParticipant).where(SceneParticipant.entity_id == str(entity_id))
            )
            await self._entities.delete(entity_id)
        await self._session.flush()


__all__ = ["MaterializedTurnOutcome", "TurnOutcomeMaterializer"]
