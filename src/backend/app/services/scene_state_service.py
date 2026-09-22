from __future__ import annotations

import json
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.location_repo import LocationRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.scene_state_table import LocationExit, SceneRuntimeState
from app.db.tables import Campaign, Character, Entity, Item, Scene, SceneParticipant
from app.services.name_identity_contract import (
    identity_display_label,
    occupied_canonical_keys,
    repair_persisted_character_identity,
)
from app.models.scene_state import (
    LocationExitCreate,
    LocationExitRead,
    SceneStateRead,
    SceneStateUpdate,
    SceneStateValidation,
)


class SceneStateService:
    """Assemble and enforce the authoritative physical state of a scene."""

    def __init__(self, session: AsyncSession):
        self._session = session
        self._locations = LocationRepository(session)
        self._scenes = SceneRepository(session)


    @staticmethod
    def _decode_custom_fields(raw: object) -> dict:
        if isinstance(raw, dict):
            return dict(raw)
        if not raw:
            return {}
        try:
            data = json.loads(raw) if isinstance(raw, str) else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _repair_character_identity_row(
        self,
        entity: Entity,
        *,
        occupied_keys: frozenset[str] | set[str] | None = None,
    ) -> str:
        """Apply shared name-identity contract to a persisted cast member.

        Prefers a short role designation over description-as-name. Never invents a
        personal name; marks needs_name status and a human-facing fail-soft label when
        repair is impossible or the short designation would collide. Never projects the
        raw needs_name token into participant_names. Returns the identity label safe for
        presence / authority surfaces.
        """
        fields = self._decode_custom_fields(entity.custom_fields)
        repair = repair_persisted_character_identity(
            canonical_name=entity.canonical_name,
            description=entity.description,
            role=fields.get("role"),
            custom_fields=fields,
            occupied_canonical_keys=occupied_keys,
            locale_text=" ".join(
                part
                for part in (
                    entity.description or "",
                    str(fields.get("role") or ""),
                    entity.canonical_name or "",
                )
                if part
            ),
        )
        if repair.changed:
            entity.canonical_name = repair.canonical_name
            entity.description = repair.description
            entity.custom_fields = json.dumps(repair.custom_fields, ensure_ascii=False)
        return identity_display_label(
            repair.canonical_name,
            description=repair.description,
            role=repair.role,
            custom_fields=repair.custom_fields,
            occupied_canonical_keys=occupied_keys,
        )

    async def ensure_runtime_state(self, scene_id: UUID) -> SceneRuntimeState:
        state = await self._session.get(SceneRuntimeState, str(scene_id))
        if state:
            return state
        scene = await self._session.get(Scene, str(scene_id))
        if not scene:
            raise ValueError("Scene not found")
        state = SceneRuntimeState(scene_id=scene.id, world_time_order=0)
        self._session.add(state)
        await self._session.flush()
        return state

    async def update(
        self,
        campaign_id: UUID,
        scene_id: UUID,
        data: SceneStateUpdate,
    ) -> SceneStateRead:
        scene = await self._session.get(Scene, str(scene_id))
        if not scene or scene.campaign_id != str(campaign_id):
            raise ValueError("Scene not found in campaign")
        runtime = await self.ensure_runtime_state(scene_id)
        for key, value in data.model_dump(exclude_unset=True).items():
            setattr(runtime, key, value)
        await self._session.flush()
        return await self.get(campaign_id, scene_id)

    async def create_exit(
        self,
        campaign_id: UUID,
        from_location_id: UUID,
        data: LocationExitCreate,
    ) -> list[LocationExitRead]:
        source = await self._require_location(campaign_id, from_location_id)
        target = await self._require_location(campaign_id, data.to_location_id)
        if source.id == target.id:
            raise ValueError("Location exit cannot point to the same location")

        await self._upsert_exit(
            campaign_id,
            source.id,
            target.id,
            label=data.label,
            direction=data.direction,
            travel_time=data.travel_time,
            access_rule=data.access_rule,
            discovered=data.discovered,
            active=data.active,
        )
        if data.bidirectional:
            await self._upsert_exit(
                campaign_id,
                target.id,
                source.id,
                label=data.reverse_label or source.canonical_name,
                direction=None,
                travel_time=data.travel_time,
                access_rule=data.access_rule,
                discovered=data.discovered,
                active=data.active,
            )
        await self._session.flush()
        return await self.list_exits(campaign_id, from_location_id, include_hidden=True)

    async def list_exits(
        self,
        campaign_id: UUID,
        from_location_id: UUID,
        *,
        include_hidden: bool = False,
    ) -> list[LocationExitRead]:
        source = Entity.__table__.alias("source_location")
        target = Entity.__table__.alias("target_location")
        query = (
            select(LocationExit, source.c.canonical_name, target.c.canonical_name)
            .join(source, source.c.id == LocationExit.from_location_id)
            .join(target, target.c.id == LocationExit.to_location_id)
            .where(
                LocationExit.campaign_id == str(campaign_id),
                LocationExit.from_location_id == str(from_location_id),
            )
            .order_by(LocationExit.label, target.c.canonical_name)
        )
        if not include_hidden:
            query = query.where(
                LocationExit.active.is_(True),
                LocationExit.discovered.is_(True),
            )
        rows = (await self._session.execute(query)).all()
        return [
            LocationExitRead(
                id=UUID(exit_row.id),
                campaign_id=UUID(exit_row.campaign_id),
                from_location_id=UUID(exit_row.from_location_id),
                to_location_id=UUID(exit_row.to_location_id),
                from_location_name=source_name,
                to_location_name=target_name,
                label=exit_row.label,
                direction=exit_row.direction,
                travel_time=exit_row.travel_time,
                access_rule=exit_row.access_rule,
                discovered=exit_row.discovered,
                active=exit_row.active,
                created_at=exit_row.created_at,
                updated_at=exit_row.updated_at,
            )
            for exit_row, source_name, target_name in rows
        ]

    async def get(self, campaign_id: UUID, scene_id: UUID) -> SceneStateRead:
        scene = await self._session.get(Scene, str(scene_id))
        if not scene or scene.campaign_id != str(campaign_id):
            raise ValueError("Scene not found in campaign")
        campaign = await self._session.get(Campaign, str(campaign_id))
        if not campaign:
            raise ValueError("Campaign not found")
        runtime = await self.ensure_runtime_state(scene_id)

        location_id = await self._scenes.get_location_id(scene_id)
        ancestry = await self._locations.ancestry(location_id) if location_id else []
        location_path = [location.canonical_name for location in ancestry]

        participant_rows = (
            await self._session.execute(
                select(Entity, Character)
                .join(SceneParticipant, SceneParticipant.entity_id == Entity.id)
                .outerjoin(Character, Character.entity_id == Entity.id)
                .where(SceneParticipant.scene_id == scene.id)
                .order_by(Entity.canonical_name)
            )
        ).all()
        participant_ids = [UUID(entity.id) for entity, _ in participant_rows]
        participant_names = []
        identity_changed = False
        # Live occupancy grows as each row is repaired so later siblings cannot twin
        # the short designation claimed by an earlier cast member.
        live_rows = [
            {"id": entity.id, "canonical_name": entity.canonical_name}
            for entity, _ in participant_rows
        ]
        for index, (entity, _) in enumerate(participant_rows):
            occupied = occupied_canonical_keys(live_rows, exclude_entity_id=entity.id)
            before = entity.canonical_name
            label = self._repair_character_identity_row(entity, occupied_keys=occupied)
            participant_names.append(label)
            live_rows[index] = {"id": entity.id, "canonical_name": entity.canonical_name}
            if entity.canonical_name != before or label != before:
                identity_changed = True
        if identity_changed:
            await self._session.flush()

        object_rows = []
        if location_id:
            object_rows = (
                await self._session.execute(
                    select(Entity)
                    .join(Item, Item.entity_id == Entity.id)
                    .where(Item.current_location_id == str(location_id))
                    .order_by(Entity.canonical_name)
                )
            ).scalars().all()

        exits = (
            await self.list_exits(campaign_id, location_id)
            if location_id
            else []
        )
        errors: list[str] = []
        if scene.status == "active" and campaign.current_scene_id != scene.id:
            errors.append("active scene is not campaign.current_scene_id")
        if scene.status == "active" and location_id is None:
            errors.append("active scene has no structured location")

        player_id = campaign.player_character_id
        if scene.status == "active" and player_id:
            if player_id not in {str(value) for value in participant_ids}:
                errors.append("player character is not a scene participant")
            player = await self._session.get(Character, player_id)
            if location_id and player and player.current_location_id != str(location_id):
                errors.append("player current_location_id differs from scene location")

        expected_location = str(location_id) if location_id else None
        for entity, character in participant_rows:
            if character is None:
                errors.append(f"participant {entity.canonical_name} has no character state")
                continue
            if expected_location and character.current_location_id != expected_location:
                errors.append(
                    f"participant {entity.canonical_name} is at "
                    f"{character.current_location_id or 'unknown'}, not scene location"
                )

        return SceneStateRead(
            campaign_id=campaign_id,
            scene_id=scene_id,
            scene_status=scene.status,
            scene_title=scene.title,
            location_id=location_id,
            location_path=location_path,
            world_time_label=runtime.world_time_label,
            world_time_order=runtime.world_time_order,
            scene_goal=runtime.scene_goal,
            active_conflict=runtime.active_conflict,
            participant_ids=participant_ids,
            participant_names=participant_names,
            object_ids=[UUID(item.id) for item in object_rows],
            object_names=[item.canonical_name for item in object_rows],
            available_exits=exits,
            invariant_errors=errors,
        )

    async def validate(
        self,
        campaign_id: UUID,
        scene_id: UUID,
    ) -> SceneStateValidation:
        state = await self.get(campaign_id, scene_id)
        return SceneStateValidation(
            valid=not state.invariant_errors,
            errors=state.invariant_errors,
            state=state,
        )

    async def require_valid(self, campaign_id: UUID, scene_id: UUID) -> SceneStateRead:
        state = await self.get(campaign_id, scene_id)
        if state.invariant_errors:
            raise ValueError("; ".join(state.invariant_errors))
        return state

    async def ensure_destination(
        self,
        campaign_id: UUID,
        source_location_id: UUID | None,
        target_location_id: UUID | None,
        *,
        allow_discovery: bool,
    ) -> None:
        if not source_location_id or not target_location_id:
            return
        if source_location_id == target_location_id:
            return
        exits = await self.list_exits(
            campaign_id,
            source_location_id,
            include_hidden=True,
        )
        direct = next(
            (
                item
                for item in exits
                if item.to_location_id == target_location_id
            ),
            None,
        )
        if direct:
            if not direct.active:
                detail = f" ({direct.access_rule})" if direct.access_rule else ""
                raise ValueError(f"Destination route is currently inactive{detail}")
            if not direct.discovered:
                if not allow_discovery:
                    raise ValueError("Destination exit has not been discovered")
                row = await self._session.get(LocationExit, str(direct.id))
                if row:
                    row.discovered = True
                    await self._session.flush()
            return
        if exits and not allow_discovery:
            names = ", ".join(item.to_location_name for item in exits if item.active)
            raise ValueError(
                "Destination is not an available exit from the current location"
                + (f". Available: {names}" if names else "")
            )

        target = await self._require_location(campaign_id, target_location_id)
        source = await self._require_location(campaign_id, source_location_id)
        await self._upsert_exit(
            campaign_id,
            source.id,
            target.id,
            label=target.canonical_name,
            direction=None,
            travel_time=None,
            access_rule=None,
            discovered=True,
            active=True,
        )
        await self._upsert_exit(
            campaign_id,
            target.id,
            source.id,
            label=source.canonical_name,
            direction=None,
            travel_time=None,
            access_rule=None,
            discovered=True,
            active=True,
        )
        await self._session.flush()

    async def inherit_transition_state(
        self,
        source_scene_id: UUID | None,
        target_scene_id: UUID,
        *,
        elapsed_time: str | None,
        time_after: str | None,
        scene_goal: str | None = None,
        active_conflict: str | None = None,
    ) -> None:
        target = await self.ensure_runtime_state(target_scene_id)
        source = (
            await self.ensure_runtime_state(source_scene_id)
            if source_scene_id
            else None
        )
        target.world_time_order = (source.world_time_order if source else 0) + 1
        target.world_time_label = (
            time_after
            or (
                f"{source.world_time_label} + {elapsed_time}"
                if source and source.world_time_label and elapsed_time
                else elapsed_time
            )
            or (source.world_time_label if source else None)
        )
        target.scene_goal = scene_goal if scene_goal is not None else (
            source.scene_goal if source else None
        )
        target.active_conflict = (
            active_conflict
            if active_conflict is not None
            else (source.active_conflict if source else None)
        )
        await self._session.flush()

    @staticmethod
    def prompt_contract(state: SceneStateRead) -> str:
        from app.services.planning_context import SCENE_RENDERER_RULES

        location = " > ".join(state.location_path) or "unknown"
        participants = ", ".join(state.participant_names) or "player only / none recorded"
        exits = ", ".join(
            f"{item.label} -> {item.to_location_name}"
            + (f" ({item.access_rule})" if item.access_rule else "")
            for item in state.available_exits
        ) or "none recorded"
        objects = ", ".join(
            f"{name} [id={object_id}]"
            for object_id, name in zip(state.object_ids, state.object_names)
        ) or "none recorded"
        return (
            "[AUTHORITATIVE SCENE STATE]\n"
            f"Scene: {state.scene_title} ({state.scene_status})\n"
            f"Location path: {location}\n"
            f"World time: {state.world_time_label or 'unspecified'} "
            f"[order {state.world_time_order}]\n"
            f"Scene goal: {state.scene_goal or 'none'}\n"
            f"Active conflict: {state.active_conflict or 'none'}\n"
            f"Physically present characters: {participants}\n"
            f"Objects physically here: {objects}\n"
            f"Available exits: {exits}\n"
            + SCENE_RENDERER_RULES
        )

    async def _upsert_exit(
        self,
        campaign_id: UUID,
        source_id: UUID,
        target_id: UUID,
        *,
        label: str,
        direction: str | None,
        travel_time: str | None,
        access_rule: str | None,
        discovered: bool,
        active: bool,
    ) -> LocationExit:
        result = await self._session.execute(
            select(LocationExit).where(
                LocationExit.from_location_id == str(source_id),
                LocationExit.to_location_id == str(target_id),
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            row = LocationExit(
                campaign_id=str(campaign_id),
                from_location_id=str(source_id),
                to_location_id=str(target_id),
                label=label,
            )
            self._session.add(row)
        row.label = label
        row.direction = direction
        row.travel_time = travel_time
        row.access_rule = access_rule
        row.discovered = discovered
        row.active = active
        await self._session.flush()
        return row

    async def _require_location(self, campaign_id: UUID, location_id: UUID):
        location = await self._locations.get_by_id(location_id)
        if not location or location.campaign_id != campaign_id:
            raise ValueError("Location not found in campaign")
        return location
