import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.location_repo import LocationRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.scene_transition_table import SceneTransition
from app.db.tables import Campaign, Character, Entity, Scene, SceneParticipant
from app.models.location import LocationCreate
from app.models.scene import SceneCreate, SceneRead
from app.services.scene_lifecycle import SceneLifecycleService
from app.services.scene_state_service import SceneStateService
from app.services.turn_planner import SceneTransitionPlan


@dataclass(frozen=True)
class AppliedSceneTransition:
    scene: SceneRead
    source_scene_id: UUID | None
    target_scene_id: UUID
    source_location_id: UUID | None
    target_location_id: UUID | None
    transition_id: UUID
    status: str


class SceneTransitionExecutor:
    """Prepare, finalize and compensate structured scene boundaries."""

    def __init__(self, session: AsyncSession):
        self._session = session
        self._scenes = SceneRepository(session)
        self._locations = LocationRepository(session)
        self._state = SceneStateService(session)

    async def existing_for_turn(
        self,
        campaign_id: UUID,
        trigger_turn_id: UUID,
    ) -> AppliedSceneTransition | None:
        result = await self._session.execute(
            select(SceneTransition)
            .where(
                SceneTransition.campaign_id == str(campaign_id),
                SceneTransition.trigger_turn_id == str(trigger_turn_id),
                SceneTransition.status.in_(("prepared", "applied")),
            )
            .order_by(SceneTransition.created_at)
            .limit(1)
        )
        row = result.scalar_one_or_none()
        if not row:
            return None
        scene = await self._scenes.get_by_id(UUID(row.target_scene_id))
        if not scene:
            return None
        if scene.status != "active":
            scene = (
                await SceneLifecycleService(self._session).activate(
                    campaign_id,
                    UUID(row.target_scene_id),
                )
            ).scene
        return self._to_applied(row, scene)

    async def apply(
        self,
        campaign_id: UUID,
        source_scene_id: UUID | None,
        trigger_turn_id: UUID | None,
        plan: SceneTransitionPlan,
    ) -> AppliedSceneTransition | None:
        if not plan.required or plan.transition_type == "none":
            return None

        if trigger_turn_id:
            existing = await self.existing_for_turn(campaign_id, trigger_turn_id)
            if existing:
                return existing

        campaign = await self._session.get(Campaign, str(campaign_id))
        if not campaign:
            raise ValueError("Campaign not found")

        source_scene = None
        if source_scene_id:
            source_scene = await self._session.get(Scene, str(source_scene_id))
            if not source_scene or source_scene.campaign_id != str(campaign_id):
                raise ValueError("Source scene not found in campaign")

        source_location_id = (
            await self._scenes.get_location_id(source_scene_id)
            if source_scene_id
            else None
        )
        target_location_id = source_location_id
        destination_created = False
        if plan.transition_type == "location_transition":
            target_location_id, destination_created = (
                await self._resolve_or_create_location(
                    campaign_id,
                    plan.destination_location or "",
                    plan.destination_parent_location,
                )
            )
            await self._state.ensure_destination(
                campaign_id,
                source_location_id,
                target_location_id,
                allow_discovery=destination_created,
            )

        target_scene = await self._scenes.create(
            campaign_id,
            SceneCreate(
                title=self._scene_title(source_scene, plan),
                location_id=target_location_id,
                location_description=None,
            ),
        )
        await self._state.inherit_transition_state(
            source_scene_id,
            target_scene.id,
            elapsed_time=plan.elapsed_time,
            time_after=plan.time_after,
        )

        participant_ids = await self._participants_to_carry(
            campaign_id,
            campaign.player_character_id,
            source_scene_id,
            plan,
        )
        for participant_id in participant_ids:
            await self._scenes.add_participant(
                target_scene.id,
                participant_id,
                allow_movement=True,
            )

        target_scene = (
            await SceneLifecycleService(self._session).activate(
                campaign_id,
                target_scene.id,
            )
        ).scene

        transition_id = uuid4()
        row = SceneTransition(
            id=str(transition_id),
            campaign_id=str(campaign_id),
            source_scene_id=(str(source_scene_id) if source_scene_id else None),
            target_scene_id=str(target_scene.id),
            trigger_turn_id=(str(trigger_turn_id) if trigger_turn_id else None),
            transition_type=plan.transition_type,
            status="prepared",
            source_location_id=(
                str(source_location_id) if source_location_id else None
            ),
            target_location_id=(
                str(target_location_id) if target_location_id else None
            ),
            elapsed_time=plan.elapsed_time,
            time_after=plan.time_after,
            reason=plan.reason,
            detector="turn_planner",
        )
        self._session.add(row)
        await self._session.flush()
        return self._to_applied(row, target_scene)

    async def mark_applied(self, transition_id: UUID) -> bool:
        row = await self._session.get(SceneTransition, str(transition_id))
        if not row:
            return False
        if row.status == "prepared":
            row.status = "applied"
            await self._session.flush()
        return row.status == "applied"

    async def rollback_transition(self, transition_id: UUID) -> bool:
        """Compensate a prepared transition after failed/cancelled narration."""
        row = await self._session.get(SceneTransition, str(transition_id))
        if not row:
            return False
        if row.status in {"rolled_back", "undone"}:
            return True
        if row.status != "prepared":
            return False

        campaign_id = UUID(row.campaign_id)
        if row.source_scene_id:
            await self._restore_scene_participant_locations(
                UUID(row.source_scene_id)
            )
            await SceneLifecycleService(self._session).activate(
                campaign_id,
                UUID(row.source_scene_id),
            )
        else:
            campaign = await self._session.get(Campaign, row.campaign_id)
            if campaign:
                campaign.current_scene_id = None
                if campaign.player_character_id:
                    player = await self._session.get(
                        Character,
                        campaign.player_character_id,
                    )
                    if player:
                        player.current_location_id = None

        target = await self._session.get(Scene, row.target_scene_id)
        if target:
            target.status = "abandoned"
        row.status = "rolled_back"
        row.undone_at = datetime.utcnow()
        await self._session.flush()
        return True

    async def _resolve_or_create_location(
        self,
        campaign_id: UUID,
        destination: str,
        parent_name: str | None,
    ) -> tuple[UUID, bool]:
        clean_destination = " ".join(destination.split())
        if not clean_destination:
            raise ValueError("Destination location is empty")

        locations = await self._locations.list_by_campaign(campaign_id)
        match = self._match_location(locations, clean_destination)
        if match:
            return match.id, False

        parent_id = None
        if parent_name:
            parent = self._match_location(
                locations,
                " ".join(parent_name.split()),
            )
            if parent:
                parent_id = parent.id

        created = await self._locations.create(
            campaign_id,
            LocationCreate(
                canonical_name=clean_destination,
                parent_location_id=parent_id,
                custom_fields={"created_by": "turn_planner"},
            ),
        )
        return created.id, True

    @staticmethod
    def _match_location(locations, name: str):
        needle = name.casefold()
        for location in locations:
            if location.canonical_name.casefold() == needle:
                return location
            if any(alias.casefold() == needle for alias in location.aliases):
                return location
        return None

    async def _participants_to_carry(
        self,
        campaign_id: UUID,
        player_character_id: str | None,
        source_scene_id: UUID | None,
        plan: SceneTransitionPlan,
    ) -> list[UUID]:
        selected: list[UUID] = []
        if player_character_id:
            selected.append(UUID(player_character_id))
        if not source_scene_id:
            return selected

        result = await self._session.execute(
            select(Entity)
            .join(SceneParticipant, SceneParticipant.entity_id == Entity.id)
            .where(
                SceneParticipant.scene_id == str(source_scene_id),
                Entity.campaign_id == str(campaign_id),
                Entity.entity_type == "character",
            )
        )
        present = result.scalars().all()
        requested = {name.casefold() for name in plan.carry_participants}
        carry_all = plan.transition_type == "focus_transition" and not requested
        for entity in present:
            entity_id = UUID(entity.id)
            if entity_id in selected:
                continue
            try:
                aliases = json.loads(entity.aliases or "[]")
            except (TypeError, json.JSONDecodeError):
                aliases = []
            names = {entity.canonical_name.casefold()}
            names.update(str(alias).casefold() for alias in aliases)
            if carry_all or names & requested:
                selected.append(entity_id)
        return selected

    async def _restore_scene_participant_locations(self, scene_id: UUID) -> None:
        location_id = await self._scenes.get_location_id(scene_id)
        if not location_id:
            return
        participant_ids = await self._scenes.get_participants(scene_id)
        for participant_id in participant_ids:
            character = await self._session.get(Character, str(participant_id))
            if character:
                character.current_location_id = str(location_id)
        await self._session.flush()

    @staticmethod
    def _scene_title(
        source_scene: Scene | None,
        plan: SceneTransitionPlan,
    ) -> str:
        if plan.scene_title:
            return plan.scene_title
        if plan.transition_type == "location_transition":
            return plan.destination_location or "Новая локация"
        source_title = source_scene.title if source_scene else "Сцена"
        if plan.transition_type == "time_transition":
            marker = plan.time_after or plan.elapsed_time or "позже"
            return f"{source_title} — {marker}"
        return f"{source_title} — новый фокус"

    @staticmethod
    def _to_applied(
        row: SceneTransition,
        scene: SceneRead,
    ) -> AppliedSceneTransition:
        return AppliedSceneTransition(
            scene=scene,
            source_scene_id=(
                UUID(row.source_scene_id) if row.source_scene_id else None
            ),
            target_scene_id=UUID(row.target_scene_id),
            source_location_id=(
                UUID(row.source_location_id) if row.source_location_id else None
            ),
            target_location_id=(
                UUID(row.target_location_id) if row.target_location_id else None
            ),
            transition_id=UUID(row.id),
            status=row.status,
        )
