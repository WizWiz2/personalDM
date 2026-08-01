import json
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.location_repo import LocationRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.scene_transition_table import SceneTransition
from app.db.tables import Campaign, Entity, Scene, SceneParticipant
from app.models.location import LocationCreate
from app.models.scene import SceneCreate, SceneRead
from app.services.scene_lifecycle import SceneLifecycleService
from app.services.turn_planner import SceneTransitionPlan


@dataclass(frozen=True)
class AppliedSceneTransition:
    scene: SceneRead
    source_scene_id: UUID | None
    target_scene_id: UUID
    source_location_id: UUID | None
    target_location_id: UUID | None
    transition_id: UUID


class SceneTransitionExecutor:
    """Apply a validated planner transition before prose generation.

    The executor is deliberately conservative: a new scene starts with the player
    character and only explicitly carried participants. A focus transition without
    an explicit list keeps the current cast because the physical interaction remains
    in place; location and time transitions never inherit the old cast implicitly.
    """

    def __init__(self, session: AsyncSession):
        self._session = session
        self._scenes = SceneRepository(session)
        self._locations = LocationRepository(session)

    async def apply(
        self,
        campaign_id: UUID,
        source_scene_id: UUID | None,
        trigger_turn_id: UUID | None,
        plan: SceneTransitionPlan,
    ) -> AppliedSceneTransition | None:
        if not plan.required or plan.transition_type == "none":
            return None

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
        if plan.transition_type == "location_transition":
            target_location_id = await self._resolve_or_create_location(
                campaign_id,
                plan.destination_location or "",
                plan.destination_parent_location,
            )

        title = self._scene_title(source_scene, plan)
        target_scene = await self._scenes.create(
            campaign_id,
            SceneCreate(
                title=title,
                location_id=target_location_id,
                location_description=None,
            ),
        )

        participant_ids = await self._participants_to_carry(
            campaign_id,
            campaign.player_character_id,
            source_scene_id,
            plan,
        )
        for participant_id in participant_ids:
            await self._scenes.add_participant(target_scene.id, participant_id)

        target_scene = (
            await SceneLifecycleService(self._session).activate(
                campaign_id,
                target_scene.id,
            )
        ).scene

        row = SceneTransition(
            campaign_id=str(campaign_id),
            source_scene_id=(str(source_scene_id) if source_scene_id else None),
            target_scene_id=str(target_scene.id),
            trigger_turn_id=(str(trigger_turn_id) if trigger_turn_id else None),
            transition_type=plan.transition_type,
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

        return AppliedSceneTransition(
            scene=target_scene,
            source_scene_id=source_scene_id,
            target_scene_id=target_scene.id,
            source_location_id=source_location_id,
            target_location_id=target_location_id,
            transition_id=UUID(row.id),
        )

    async def _resolve_or_create_location(
        self,
        campaign_id: UUID,
        destination: str,
        parent_name: str | None,
    ) -> UUID:
        clean_destination = " ".join(destination.split())
        if not clean_destination:
            raise ValueError("Destination location is empty")

        locations = await self._locations.list_by_campaign(campaign_id)
        match = self._match_location(locations, clean_destination)
        if match:
            return match.id

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
        return created.id

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
        carry_all = (
            plan.transition_type == "focus_transition"
            and not requested
        )
        for entity in present:
            entity_id = UUID(entity.id)
            if entity_id in selected:
                continue
            aliases = []
            try:
                aliases = json.loads(entity.aliases or "[]")
            except (TypeError, json.JSONDecodeError):
                aliases = []
            names = {entity.canonical_name.casefold()}
            names.update(str(alias).casefold() for alias in aliases)
            if carry_all or names & requested:
                selected.append(entity_id)
        return selected

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
