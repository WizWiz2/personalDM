from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.fact_repo import FactRepository
from app.db.repositories.job_repo import PostTurnJobRepository
from app.db.repositories.location_repo import LocationRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.repositories.turn_repo import TurnRepository
from app.db.tables import Turn
from app.models.character import CharacterCreate, CharacterRead
from app.models.fact import FactRead
from app.models.location import LocationCreate
from app.models.scene import SceneCreate, SceneRead
from app.models.turn import TurnCreate
from app.runtime import install_runtime
from app.services.meta_command_router import MetaCommandRunner, parse_meta_command
from app.services.post_turn_processor import PostTurnProcessor
from app.services.scene_lifecycle import SceneLifecycleService
from app.services.session_zero_service import (
    SessionZeroIncompleteError,
    SessionZeroService,
)
from app.services.turn_runner import TurnRunner
from app.services.turn_undo_service import TurnUndoService


class CampaignNotFoundError(ValueError):
    pass


class CurrentSceneError(ValueError):
    pass


class GameNotReadyError(ValueError):
    def __init__(self, missing_fields: list[str]):
        self.missing_fields = missing_fields
        super().__init__("Session zero is incomplete")


class TurnNotFoundError(ValueError):
    pass


class TurnRegenerationError(ValueError):
    pass


@dataclass(frozen=True)
class GameInputRoute:
    channel: Literal["narrative", "meta"]
    stream: AsyncIterator[str]


@dataclass(frozen=True)
class ParticipantView:
    id: UUID
    name: str


@dataclass(frozen=True)
class GameSceneView:
    campaign_name: str
    scene: SceneRead
    player_character_id: UUID | None
    participants: tuple[ParticipantView, ...]
    npcs: tuple[ParticipantView, ...]


@dataclass(frozen=True)
class GamePostTurnStatus:
    failed_count: int
    rate_limited: bool


@dataclass(frozen=True)
class RetryPostTurnResult:
    succeeded: int
    remaining: int


class GameApplication:
    """The single application boundary used by CLI and HTTP adapters.

    UI code may format input and output, but it must not choose a different narrator,
    meta-command, undo, scene-lifecycle or post-turn pipeline. All runtime composition
    is installed here before any operation is routed.
    """

    def __init__(self, session: AsyncSession):
        install_runtime()
        self._session = session
        self._campaigns = CampaignRepository(session)
        self._entities = EntityRepository(session)
        self._facts = FactRepository(session)
        self._jobs = PostTurnJobRepository(session)
        self._locations = LocationRepository(session)
        self._scenes = SceneRepository(session)
        self._turns = TurnRepository(session)

    async def route_input(
        self,
        campaign_id: UUID,
        data: TurnCreate,
        *,
        existing_user_turn_id: UUID | None = None,
    ) -> GameInputRoute:
        if data.role != "user":
            raise ValueError("The game application accepts only role='user'")

        command = parse_meta_command(data.content)
        if command:
            if not await self._campaigns.get_by_id(campaign_id):
                raise CampaignNotFoundError("Campaign not found")
            return GameInputRoute(
                channel="meta",
                stream=MetaCommandRunner(self._session).run_stream(
                    campaign_id,
                    command,
                ),
            )

        bound = await self._bind_current_scene(campaign_id, data)
        return GameInputRoute(
            channel="narrative",
            stream=TurnRunner(self._session).run_turn_stream(
                campaign_id,
                bound,
                existing_user_turn_id=existing_user_turn_id,
            ),
        )

    async def regenerate_turn(
        self,
        campaign_id: UUID,
        assistant_turn_id: UUID,
    ) -> GameInputRoute:
        result = await self._session.execute(
            select(Turn).where(
                Turn.id == str(assistant_turn_id),
                Turn.campaign_id == str(campaign_id),
            )
        )
        assistant = result.scalar_one_or_none()
        if not assistant or assistant.role != "assistant":
            raise TurnNotFoundError("Narrative assistant turn to regenerate not found")
        if not assistant.parent_turn_id:
            raise TurnRegenerationError(
                "Cannot regenerate a turn without a parent user turn"
            )

        user_turn = await self._turns.get_by_id(UUID(assistant.parent_turn_id))
        if not user_turn or user_turn.campaign_id != campaign_id:
            raise TurnNotFoundError("Parent user turn not found")

        actor_id = None
        if assistant.context_snapshot:
            try:
                snapshot = json.loads(assistant.context_snapshot)
                actor_value = snapshot.get("acting_character_id")
                if actor_value:
                    actor_id = UUID(actor_value)
            except (json.JSONDecodeError, ValueError, TypeError):
                actor_id = None

        await self._turns.mark_alternative(assistant_turn_id)
        await self._session.commit()
        regeneration_input = TurnCreate(
            role="user",
            content=user_turn.content,
            scene_id=user_turn.scene_id,
            acting_character_id=actor_id,
            parent_turn_id=user_turn.parent_turn_id,
            model_name=user_turn.model_name,
        )
        return GameInputRoute(
            channel="narrative",
            stream=TurnRunner(self._session).run_turn_stream(
                campaign_id,
                regeneration_input,
                existing_user_turn_id=user_turn.id,
            ),
        )

    async def _bind_current_scene(
        self,
        campaign_id: UUID,
        data: TurnCreate,
    ) -> TurnCreate:
        campaign = await self._campaigns.get_by_id(campaign_id)
        if not campaign:
            raise CampaignNotFoundError("Campaign not found")
        try:
            await SessionZeroService(self._session).require_completed(campaign_id)
        except SessionZeroIncompleteError as exc:
            raise GameNotReadyError(exc.missing_fields) from exc

        effective_scene_id = campaign.current_scene_id or data.scene_id
        if effective_scene_id:
            scene = await self._scenes.get_by_id(effective_scene_id)
            if not scene or scene.campaign_id != campaign_id:
                raise CurrentSceneError(
                    "Campaign current scene is missing or belongs to another campaign"
                )
        return data.model_copy(update={"scene_id": effective_scene_id})

    async def undo_last_turn(self, campaign_id: UUID) -> bool:
        try:
            success = await TurnUndoService(self._session).undo_last_pair(campaign_id)
            if success:
                await self._session.commit()
            return success
        except Exception:
            await self._session.rollback()
            raise

    async def stop_generation(self, campaign_id: UUID) -> bool:
        return await TurnRunner.stop_generation(campaign_id, self._session)

    async def retry_failed_post_turn(
        self,
        campaign_id: UUID,
        *,
        limit: int = 200,
    ) -> RetryPostTurnResult:
        jobs = await self._jobs.list_for_campaign(campaign_id, limit=limit)
        failed = [job for job in jobs if job.status == "failed"]
        processor = PostTurnProcessor(self._session)
        succeeded = 0
        remaining = 0
        for job in reversed(failed):
            await self._jobs.retry(job.id)
            await self._session.commit()
            try:
                await processor.process_job(job.id)
                succeeded += 1
            except Exception:  # noqa: BLE001 - application retry boundary
                # process_job has already persisted a durable failed state.
                remaining += 1
        return RetryPostTurnResult(succeeded=succeeded, remaining=remaining)

    async def post_turn_status(self, assistant_turn_id: UUID) -> GamePostTurnStatus:
        jobs = await self._jobs.list_for_turn(assistant_turn_id)
        failed = [job for job in jobs if job.status == "failed"]
        return GamePostTurnStatus(
            failed_count=len(failed),
            rate_limited=any(
                self.is_rate_limited_error(job.error) for job in failed
            ),
        )

    async def latest_assistant_turn_id(self, campaign_id: UUID) -> UUID | None:
        history = await self._turns.get_history(campaign_id, limit=20, channel="all")
        assistants = [item for item in history if item.role == "assistant"]
        return assistants[-1].id if assistants else None

    async def current_scene_view(self, campaign_id: UUID) -> GameSceneView | None:
        campaign = await self._campaigns.get_by_id(campaign_id)
        if not campaign:
            raise CampaignNotFoundError("Campaign not found")
        if not campaign.current_scene_id:
            return None
        scene = await self._scenes.get_by_id(campaign.current_scene_id)
        if not scene:
            raise CurrentSceneError("Campaign current scene is missing")
        characters = await self._entities.get_characters_in_scene(scene.id)
        participants = tuple(
            ParticipantView(id=item.id, name=item.canonical_name)
            for item in characters
        )
        npcs = tuple(
            item
            for item in participants
            if item.id != campaign.player_character_id
        )
        return GameSceneView(
            campaign_name=campaign.name,
            scene=scene,
            player_character_id=campaign.player_character_id,
            participants=participants,
            npcs=npcs,
        )

    async def list_active_facts(self, campaign_id: UUID) -> list[FactRead]:
        return await self._facts.list_active(campaign_id)

    async def create_character(
        self,
        campaign_id: UUID,
        data: CharacterCreate,
    ) -> CharacterRead:
        try:
            result = await self._entities.create_character(campaign_id, data)
            await self._session.commit()
            return result
        except Exception:
            await self._session.rollback()
            raise

    async def create_and_activate_scene(
        self,
        campaign_id: UUID,
        *,
        location_name: str,
        description: str,
        mood: str,
    ) -> SceneRead:
        try:
            location = await self._locations.create(
                campaign_id,
                LocationCreate(
                    canonical_name=location_name,
                    description=description,
                    atmosphere=mood,
                ),
            )
            scene = await self._scenes.create(
                campaign_id,
                SceneCreate(
                    title=location_name,
                    location_id=location.id,
                    mood=mood,
                ),
            )
            activated = await SceneLifecycleService(self._session).activate(
                campaign_id,
                scene.id,
            )
            await self._session.commit()
            return activated.scene
        except Exception:
            await self._session.rollback()
            raise

    async def participant_roster(
        self,
        campaign_id: UUID,
    ) -> tuple[GameSceneView, tuple[ParticipantView, ...]]:
        view = await self.current_scene_view(campaign_id)
        if view is None:
            raise CurrentSceneError("Campaign has no active scene")
        all_characters = await self._entities.list_by_campaign(
            campaign_id,
            entity_type="character",
        )
        participant_ids = {participant.id for participant in view.participants}
        available = tuple(
            ParticipantView(id=item.id, name=item.canonical_name)
            for item in all_characters
            if item.id not in participant_ids
        )
        return view, available

    async def add_participant(
        self,
        campaign_id: UUID,
        entity_id: UUID,
    ) -> None:
        view = await self.current_scene_view(campaign_id)
        if view is None:
            raise CurrentSceneError("Campaign has no active scene")
        try:
            # This is an explicit admin operation, so moving the selected character
            # into the active location is intentional rather than an implicit teleport.
            await self._scenes.add_participant(
                view.scene.id,
                entity_id,
                allow_movement=True,
            )
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise

    async def remove_participant(
        self,
        campaign_id: UUID,
        entity_id: UUID,
    ) -> bool:
        view = await self.current_scene_view(campaign_id)
        if view is None:
            raise CurrentSceneError("Campaign has no active scene")
        if entity_id == view.player_character_id:
            raise ValueError(
                "The player character cannot be removed from the active scene"
            )
        try:
            removed = await self._scenes.remove_participant(view.scene.id, entity_id)
            await self._session.commit()
            return removed
        except Exception:
            await self._session.rollback()
            raise

    @staticmethod
    def is_rate_limited_error(error: str | None) -> bool:
        text = (error or "").casefold()
        return "429" in text or "rate limit" in text or "rate_limit" in text
