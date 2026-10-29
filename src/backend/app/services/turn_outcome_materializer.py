from __future__ import annotations

import json
from dataclasses import dataclass, replace
from uuid import UUID, uuid5

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import delete, func, select

from app.db.generation_lifecycle_table import ProvisionalTurnEffect

from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.location_repo import LocationRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.tables import Character, Entity, Location, Item, ItemContentDisclosure, ItemCreationEffect, NpcIdentityAnchor, SceneParticipant, Turn
from app.db.action_sequence_table import ActionSequence, ActionStep
from app.services.item_content_authority import revealed_item_contents
from app.models.addressed_response import AddressedResponse
from app.models.character import CharacterCreate
from app.models.entity import EntityCreate, EntityType
from app.models.location import LocationCreate
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
    introduced_item_ids: tuple[UUID, ...] = ()
    introduced_place_ids: tuple[UUID, ...] = ()
    arrived_existing_participants: tuple[tuple[UUID, UUID], ...] = ()
    identity_updates: tuple[IdentityUpdate, ...] = ()

    @property
    def arrived_existing_character_ids(self) -> tuple[UUID, ...]:
        return tuple(entity_id for _scene_id, entity_id in self.arrived_existing_participants)

    @property
    def has_changes(self) -> bool:
        return bool(
            self.introduced_character_ids or self.introduced_item_ids or self.introduced_place_ids
            or self.arrived_existing_participants or self.identity_updates
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
        if not (
            authority.allowed_new_npcs or authority.allowed_new_items or authority.allowed_new_places
            or authority.allowed_existing_npc_arrivals or reveal_requested
        ):
            return MaterializedTurnOutcome()
        if not authority.target_scene_id:
            raise ValueError("Planned NPC materialization has no authoritative target scene")

        existing_participants = set(await self._scenes.get_participants(authority.target_scene_id))
        arrived_existing: list[tuple[UUID, UUID]] = []
        for arrival in authority.allowed_existing_npc_arrivals:
            if arrival.entity_id in existing_participants:
                continue
            # Authority checked the character is at this establishment or a completed step brings them.
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

        item_ids: list[UUID] = []
        disclosed_contents = (await revealed_item_contents(self._session, authority.campaign_id)
                              if any(item.copy_of_item_id for item in authority.allowed_new_items) else {})
        known_items = await self._entities.list_by_campaign(
            authority.campaign_id, entity_type="item",
        )
        occupied = {
            identity_key(name)
            for entity in known_items
            for name in (entity.canonical_name, *entity.aliases)
        }
        from app.db.repositories.scene_repo import SceneRepository
        location_id = await SceneRepository(self._session).get_location_id(
            authority.target_scene_id
        )
        for introduction in authority.allowed_new_items:
            if introduction.reserved_item_id is not None:
                executed = (await self._session.execute(
                    select(ItemCreationEffect.item_id)
                    .join(ActionStep, ActionStep.id == ItemCreationEffect.source_step_id)
                    .join(ActionSequence, ActionSequence.id == ActionStep.sequence_id)
                    .where(ItemCreationEffect.item_id == str(introduction.reserved_item_id),
                           ActionSequence.campaign_id == str(authority.campaign_id),
                           ActionSequence.trigger_turn_id == str(source_turn_id),
                           ActionSequence.status.in_(("prepared", "applied")), ActionStep.status == "completed")
                )).scalar_one_or_none()
                if executed:
                    item_ids.append(UUID(executed))
                    continue
                raise ValueError("Reserved item introduction has no completed creation receipt")
            source_step = None
            item_location_id = location_id
            if introduction.after_action_index is not None:
                source_step = (await self._session.execute(
                    select(ActionStep).join(ActionSequence, ActionSequence.id == ActionStep.sequence_id)
                    .where(ActionSequence.campaign_id == str(authority.campaign_id),
                           ActionSequence.trigger_turn_id == str(source_turn_id),
                           ActionSequence.status.in_(("prepared", "applied")),
                           ActionStep.status == "completed",
                           func.coalesce(ActionStep.source_action_index, ActionStep.step_index) == introduction.after_action_index)
                    .order_by(ActionStep.step_index).limit(1)
                )).scalar_one_or_none()
                if source_step and source_step.target_scene_id:
                    item_location_id = await self._scenes.get_location_id(UUID(source_step.target_scene_id))
            key = identity_key(introduction.canonical_name)
            if key in occupied:
                continue  # Repeated evidence refers to the same object, never a new clone.
            if introduction.owned_by_player and not authority.player_character_id:
                continue
            item = await self._entities.create(
                authority.campaign_id,
                EntityCreate(
                    entity_type=EntityType.ITEM,
                    canonical_name=introduction.canonical_name,
                    description=introduction.description,
                    custom_fields={
                        "introduced_by": "turn_authority",
                        "introduction_turn_id": str(source_turn_id),
                        "introduction_evidence": introduction.evidence,
                    },
                ),
            )
            self._session.add(
                Item(
                    entity_id=str(item.id),
                    current_owner_id=(
                        str(authority.player_character_id)
                        if introduction.owned_by_player else None
                    ),
                    current_location_id=(
                        None if introduction.owned_by_player
                        else str(item_location_id) if item_location_id else None
                    ),
                    is_unique=True,
                    placement_detail=(
                        getattr(introduction, "placement_detail", None)
                        if not introduction.owned_by_player else None
                    ),
                )
            )
            if introduction.copy_of_item_id and str(introduction.copy_of_item_id) in disclosed_contents:
                if source_step:
                    await self._session.flush()
                    self._session.add(ItemContentDisclosure(source_step_id=source_step.id, item_id=str(item.id),
                        content=disclosed_contents[str(introduction.copy_of_item_id)][0]))
            item_ids.append(item.id)
            occupied.add(key)
        place_ids = []
        places = LocationRepository(self._session)
        taken = {identity_key(place.canonical_name) for place in await places.list_by_campaign(authority.campaign_id)
                 if place.status != "inactive"}
        for place in authority.allowed_new_places:
            if identity_key(place.canonical_name) not in taken:  # a known name is that known place
                place_ids.append((await places.create(authority.campaign_id, LocationCreate(
                    canonical_name=place.canonical_name, description=place.profile,
                    parent_location_id=place.parent_location_id,
                    custom_fields={"created_by": "world_outcome", "introduction_turn_id": str(source_turn_id)},
                ))).id)
        await self._session.flush()
        identity_update = await self._reveal_identity(authority, source_turn_id)
        return await self._journal(source_turn_id, MaterializedTurnOutcome(
            introduced_character_ids=tuple(created_ids),
            introduced_item_ids=tuple(item_ids),
            introduced_place_ids=tuple(place_ids),
            arrived_existing_participants=tuple(arrived_existing),
            identity_updates=(identity_update,) if identity_update else (),
        ))

    async def _journal(self, trigger_turn_id: UUID, delta: MaterializedTurnOutcome) -> MaterializedTurnOutcome:
        """Record each change durably in the same transaction, so compensation needs no memory."""
        rows = [
            *(("character", e, None, None) for e in delta.introduced_character_ids),
            *(("item", e, None, None) for e in delta.introduced_item_ids),
            *(("place", e, None, None) for e in delta.introduced_place_ids),
            *(("arrival", e, scene, None) for scene, e in delta.arrived_existing_participants),
            *(("identity", u.entity_id, None, json.dumps(u.snapshot(), ensure_ascii=False))
              for u in delta.identity_updates),
        ]
        self._session.add_all(ProvisionalTurnEffect(
            trigger_turn_id=str(trigger_turn_id), kind=kind, entity_id=str(entity_id),
            scene_id=str(scene) if scene else None, previous=previous,
        ) for kind, entity_id, scene, previous in rows)
        await self._session.flush()
        return delta

    async def _create(self, authority: TurnAuthority, introduction, source_turn_id: UUID):
        # An undone NPC introduction can be re-published in a new turn.
        # Reserve its ID by campaign + normalized identity, not by the new
        # random turn ID: external GM links and client caches then remain valid.
        alias_key = identity_key(introduction.canonical_name)
        anchor = await self._session.get(
            NpcIdentityAnchor, (str(authority.campaign_id), alias_key),
        )
        stable_id = (
            UUID(anchor.entity_id) if anchor
            else uuid5(authority.campaign_id, f"personaldm:npc:{alias_key}")
        )
        # An alias of a still-present person must resolve to that person,
        # never silently collide with or spawn another physical NPC.
        if await self._session.get(Entity, str(stable_id)):
            raise ValueError("NPC stable identity already belongs to an existing entity")
        character = await self._entities.create_character(
            authority.campaign_id,
            CharacterCreate(
                canonical_name=introduction.canonical_name,
                description=introduction.description or introduction.role,
                appearance=introduction.appearance,
                voice=introduction.voice,
                current_intentions=(
                    [introduction.current_goal]
                    if introduction.current_goal else []
                ),
                custom_fields={
                    "introduced_by": "turn_authority",
                    "introduction_turn_id": str(source_turn_id),
                    "introduction_trigger_turn_id": str(authority.trigger_turn_id),
                    "introduction_reason": introduction.reason,
                    "role": introduction.role,
                    "temporary_name": introduction.temporary_name,
                    **({"slot_id": introduction.resident_slot} if introduction.resident_slot else {}),
                    **({"sent_for": True} if introduction.sent_for else {}),
                },
            ),
            stable_entity_id=stable_id,
        )
        if anchor is None:
            self._session.add(
                NpcIdentityAnchor(
                    campaign_id=str(authority.campaign_id),
                    identity_key=alias_key,
                    entity_id=str(character.id),
                )
            )
        if introduction.sent_for:
            # Offscreen in the wider place this scene belongs to, until the world brings them.
            here = await self._session.get(Location, str(await self._scenes.get_location_id(authority.target_scene_id)))
            row = await self._session.get(Character, str(character.id))
            row.current_location_id = (here.parent_location_id or here.entity_id) if here else None
            return character
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
        if outcome.introduced_character_ids:
            return outcome  # This turn's typed newcomer is the one the prose shows (replay15 r6 «Путник 2»).
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
        await self._journal(source_turn_id, MaterializedTurnOutcome(introduced_character_ids=(character.id,)))
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
            update = await self._reveal_identity(
                authority, source_turn_id, confirmed_by_published_beat=True,
            )
        except ValueError:
            return outcome  # An established personal name is never overwritten.
        if not update:
            return outcome
        await self._journal(source_turn_id, MaterializedTurnOutcome(identity_updates=(update,)))
        return replace(outcome, identity_updates=(*outcome.identity_updates, update))

    async def _reveal_identity(
        self, authority, source_turn_id, *, confirmed_by_published_beat=False,
    ) -> IdentityUpdate | None:
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
        if not fields.get("temporary_name") and not confirmed_by_published_beat:
            return None  # Planner intent is not confirmation.
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
        # Persist both established identities beyond an undo of the name reveal.
        # This is an identity anchor, not a new character or a canon fact.
        for value in (old_name, response.revealed_name):
            alias_key = identity_key(value)
            anchor = await self._session.get(
                NpcIdentityAnchor, (str(authority.campaign_id), alias_key),
            )
            if anchor is not None and anchor.entity_id != row.id:
                raise ValueError("The confirmed NPC alias belongs to another identity")
            if anchor is None:
                self._session.add(
                    NpcIdentityAnchor(
                        campaign_id=str(authority.campaign_id),
                        identity_key=alias_key,
                        entity_id=row.id,
                    )
                )
        if not fields.get("temporary_name"):
            if identity_key(response.revealed_name) in {
                identity_key(name) for name in aliases
            }:
                return None
            # Alternate confirmed name of the same physical NPC; no duplicate entity.
            aliases.append(response.revealed_name)
            row.aliases = json.dumps(aliases, ensure_ascii=False)
            await self._session.flush()
            return update
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

    async def bind_to_assistant(self, trigger_turn_id: UUID, assistant_turn_id: UUID) -> None:
        """Confirm this turn's provisional changes and bind them to the durable assistant turn."""
        for effect in await self._provisional(trigger_turn_id):
            row = await self._session.get(Entity, effect.entity_id)
            field = {"character": "introduction_turn_id", "item": "introduction_turn_id",
                     "identity": "identity_promoted_turn_id"}.get(effect.kind)
            if row and field:
                fields = json.loads(row.custom_fields or "{}")
                fields[field] = str(assistant_turn_id)
                row.custom_fields = json.dumps(fields, ensure_ascii=False)
            effect.status = "confirmed"
        await self._session.flush()

    async def _provisional(self, trigger_turn_id: UUID) -> list[ProvisionalTurnEffect]:
        return list((await self._session.execute(select(ProvisionalTurnEffect).where(
            ProvisionalTurnEffect.trigger_turn_id == str(trigger_turn_id),
            ProvisionalTurnEffect.status == "provisional",
        ).order_by(ProvisionalTurnEffect.id.desc()))).scalars())

    async def rollback(self, trigger_turn_id: UUID) -> None:
        """Compensate this turn's provisional changes from the durable journal (idempotent)."""
        for effect in await self._provisional(trigger_turn_id):
            entity_id = UUID(effect.entity_id)
            if effect.kind == "identity":
                row = await self._session.get(Entity, effect.entity_id)
                if row:
                    previous = json.loads(effect.previous)
                    row.canonical_name = previous["previous_name"]
                    row.aliases = previous["previous_aliases"]
                    row.custom_fields = previous["previous_custom_fields"]
            elif effect.kind == "arrival":
                # Existing characters are never deleted; remove only this turn's participation.
                await self._scenes.remove_participant(UUID(effect.scene_id), entity_id)
            elif effect.kind == "place":
                row = await self._session.get(Entity, effect.entity_id)
                if row:
                    row.status = "inactive"  # as undo does
            else:
                # SQLite may not enforce FK cascades: remove dependents explicitly, no dangling refs.
                await self._session.execute(delete(SceneParticipant).where(
                    SceneParticipant.entity_id == effect.entity_id))
                await self._session.execute(delete(ItemContentDisclosure).where(
                    ItemContentDisclosure.item_id == effect.entity_id))
                item = await self._session.get(Item, effect.entity_id)
                if item:
                    await self._session.delete(item)
                await self._entities.delete(entity_id)
            effect.status = "rolled_back"
        await self._session.flush()


__all__ = ["MaterializedTurnOutcome", "TurnOutcomeMaterializer"]
