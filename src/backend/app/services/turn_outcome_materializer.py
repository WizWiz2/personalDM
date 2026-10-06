from __future__ import annotations

import json
from dataclasses import dataclass, replace
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import delete, select

from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.tables import Entity, SceneParticipant, Turn
from app.models.addressed_response import AddressedResponse
from app.models.character import CharacterCreate
from app.models.narration_validation import GrantedBeat
from app.models.turn_authority import PlannedNpcIntroduction, TurnAuthority
from app.services.entity_identity import identity_key
from app.services.name_identity_contract import NEEDS_NAME_FIELD, given_name_collides
from app.services.turn_authority_resolvers import AuthorityResolutionError, NpcIntroductionResolver


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
            # Authority already checked the character is at the target place or a parent/child place
            # of it; stepping within one establishment is not a trip.
            await self._scenes.add_participant(
                authority.target_scene_id,
                arrival.entity_id,
                allow_movement=True,
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
            character = await self._create(authority, introduction, source_turn_id)
            created_ids.append(character.id)
            if authority.beat_owner_id is None and identity_key(authority.beat_owner_name or "") == key:
                authority.beat_owner_id = authority.acting_character_id = character.id
                if authority.addressed_response:
                    authority.addressed_response.speaker_id = character.id
            known_names.add(key)

        await self._session.flush()
        identity_update = await self._reveal_identity(authority, source_turn_id)
        return MaterializedTurnOutcome(
            introduced_character_ids=tuple(created_ids),
            arrived_existing_participants=tuple(arrived_existing),
            identity_updates=(identity_update,) if identity_update else (),
        )

    async def _create(self, authority: TurnAuthority, introduction, source_turn_id: UUID):
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
                    **({"slot_id": introduction.resident_slot} if introduction.resident_slot else {}),
                },
            ),
        )
        await self._scenes.add_participant(
            authority.target_scene_id,
            character.id,
            allow_movement=True,
        )
        if character.canonical_name not in authority.present_character_names:
            authority.present_character_names.append(character.canonical_name)
        return character

    async def introduce_published_newcomer(self, authority: TurnAuthority, beat, prose, outcome, source_turn_id):
        """A person a present beat owner brings in through an executed step, who then speaks or acts
        in the published prose, joins the scene by the planned-introduction path (7b T7 «речник»)."""
        newcomer = beat.newcomer if beat else None
        if not (newcomer and authority.beat_owner_id and authority.target_scene_id) or not any(
            step["status"] == "completed" for step in authority.executed_steps()
        ):
            return outcome
        claim = GrantedBeat(cast_id=newcomer.name, kind=newcomer.kind, evidence=newcomer.evidence)
        if claim.failure(newcomer.name, newcomer.name, prose):
            return outcome
        known = await self._entities.list_by_campaign(authority.campaign_id, entity_type="character")
        occupied = {identity_key(name) for entity in known for name in (entity.canonical_name, *entity.aliases)}
        try:
            [introduction] = NpcIntroductionResolver.sanitize_introductions(
                [PlannedNpcIntroduction(canonical_name=newcomer.name, role=newcomer.name, temporary_name=True,
                                        reason=f"Приведён: {authority.beat_owner_name}")],
                occupied_canonical_keys=occupied, locale_text=authority.player_input,
            )
        except (AuthorityResolutionError, ValueError):
            return outcome
        if identity_key(introduction.canonical_name) in occupied:
            return outcome
        character = await self._create(authority, introduction, source_turn_id)
        await self._session.flush()
        return replace(outcome, introduced_character_ids=(*outcome.introduced_character_ids, character.id))

    async def reveal_published_name(self, authority: TurnAuthority, beat, outcome, source_turn_id):
        """The narrator voices the beat owner, so a name it types in the owner's own published beat is
        that owner's (B5 T1 «Степаном меня зовут» never left «Рыбак у пристани»)."""
        if not (beat and beat.revealed_name and authority.beat_owner_id) or beat.revealed_name not in beat.evidence:
            return outcome
        authority.addressed_response = (authority.addressed_response or AddressedResponse()).model_copy(
            update={"speaker_id": authority.beat_owner_id, "speaker_name": authority.beat_owner_name,
                    "revealed_name": beat.revealed_name, "name_evidence": beat.evidence})
        try:
            update = await self._reveal_identity(authority, source_turn_id)
        except ValueError:
            return outcome  # An established personal name is never overwritten.
        return replace(outcome, identity_updates=(*outcome.identity_updates, update)) if update else outcome

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
        taken = {
            identity_key(name) for entity in known if entity.id != response.speaker_id
            for name in (entity.canonical_name, *entity.aliases)
        }
        if given_name_collides(response.revealed_name, taken):
            return None  # Another identity owns this name or its given name; keep the designation.
        update = IdentityUpdate(
            response.speaker_id, row.canonical_name, row.aliases or "[]", row.custom_fields or "{}",
        )
        old_name = row.canonical_name
        aliases = json.loads(row.aliases or "[]")
        # A designation becomes an alias only if the player read it; a planner's role label
        # («Хозяин или служащий трактира») never shown in prose is no name.
        shown = " ".join([response.name_evidence or "", *(await self._session.execute(
            select(Turn.content).where(Turn.campaign_id == str(authority.campaign_id),
                                       Turn.role == "assistant", Turn.status == "active")
        )).scalars()]).casefold()
        if old_name not in aliases and old_name.casefold() in shown:
            aliases.append(old_name)
        row.canonical_name = response.revealed_name
        row.aliases = json.dumps(aliases, ensure_ascii=False)
        fields.update(
            temporary_name=False, identity_promoted_from=old_name,
            identity_promoted_turn_id=str(source_turn_id),
        )
        fields.pop(NEEDS_NAME_FIELD, None)  # A personal name answers it (A10 T14: label stayed the role).
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
        authority.addressed_response = response.model_copy(update={"speaker_name": response.revealed_name})
        authority.beat_owner_name = authority.acting_character_name = response.revealed_name
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
