from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.turn_repo import TurnRepository
from app.db.scene_transition_table import SceneTransition
from app.db.tables import Campaign, Character, Scene
from app.services.scene_lifecycle import SceneLifecycleService


class TurnUndoService:
    """Undo the latest user/assistant pair and its structured scene boundary."""

    def __init__(self, session: AsyncSession):
        self._session = session
        self._turns = TurnRepository(session)

    async def undo_last_pair(self, campaign_id: UUID) -> bool:
        turns = await self._turns.get_history(
            campaign_id,
            limit=2,
            active_only=True,
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

        if transition:
            if transition.source_scene_id:
                await SceneLifecycleService(self._session).activate(
                    campaign_id,
                    UUID(transition.source_scene_id),
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
