from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.scene_repo import SceneRepository
from app.db.repositories.turn_repo import TurnRepository
from app.db.scene_transition_table import SceneTransition
from app.db.tables import Campaign, Character, Scene
from app.services.action_sequence_executor import ActionSequenceExecutor
from app.services.scene_lifecycle import SceneLifecycleService


class TurnUndoService:
    """Undo the latest user/assistant pair and every structured boundary it caused."""

    def __init__(self, session: AsyncSession):
        self._session = session
        self._turns = TurnRepository(session)
        self._scenes = SceneRepository(session)

    async def undo_last_pair(self, campaign_id: UUID) -> bool:
        turns = await self._turns.get_history(
            campaign_id,
            limit=2,
            active_only=True,
            channel="narrative",
        )
        if len(turns) != 2:
            return False
        user_turn, assistant_turn = turns
        if (
            user_turn.role != "user"
            or assistant_turn.role != "assistant"
            or assistant_turn.parent_turn_id != user_turn.id
        ):
            return False

        sequence_executor = ActionSequenceExecutor(self._session)
        action_sequence = await sequence_executor.find_applied_for_turn(
            campaign_id,
            user_turn.id,
        )
        transition = None
        if action_sequence is None:
            transition = (
                await self._session.execute(
                    select(SceneTransition)
                    .where(
                        SceneTransition.campaign_id == str(campaign_id),
                        SceneTransition.trigger_turn_id == str(user_turn.id),
                        SceneTransition.status == "applied",
                    )
                    .order_by(SceneTransition.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()

        if not await self._turns.undo_last_pair(campaign_id):
            return False

        if action_sequence:
            if not await sequence_executor.undo_applied(
                action_sequence.sequence_id
            ):
                raise RuntimeError("Applied action sequence could not be undone")
            parent = (
                await self._session.execute(
                    select(SceneTransition).where(
                        SceneTransition.campaign_id == str(campaign_id),
                        SceneTransition.trigger_turn_id == str(user_turn.id),
                        SceneTransition.detector == "compound_action_executor",
                        SceneTransition.status == "applied",
                    )
                )
            ).scalar_one_or_none()
            if parent:
                parent.status = "undone"
                parent.undone_at = datetime.utcnow()
            await self._session.flush()
            return True

        if transition:
            if transition.source_scene_id:
                source_scene_id = UUID(transition.source_scene_id)
                await self._restore_scene_participant_locations(source_scene_id)
                await SceneLifecycleService(self._session).activate(
                    campaign_id,
                    source_scene_id,
                )
            else:
                campaign = await self._session.get(Campaign, str(campaign_id))
                if campaign:
                    campaign.current_scene_id = None
                    if campaign.player_character_id:
                        player = await self._session.get(
                            Character,
                            campaign.player_character_id,
                        )
                        if player:
                            player.current_location_id = None

            target = await self._session.get(Scene, transition.target_scene_id)
            if target:
                target.status = "abandoned"
            transition.status = "undone"
            transition.undone_at = datetime.utcnow()
            await self._session.flush()

        return True

    async def _restore_scene_participant_locations(self, scene_id: UUID) -> None:
        location_id = await self._scenes.get_location_id(scene_id)
        if not location_id:
            return
        for participant_id in await self._scenes.get_participants(scene_id):
            character = await self._session.get(Character, str(participant_id))
            if character:
                character.current_location_id = str(location_id)
        await self._session.flush()
