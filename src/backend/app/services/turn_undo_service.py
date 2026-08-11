import json
from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.scene_repo import SceneRepository
from app.db.repositories.turn_repo import TurnRepository
from app.db.scene_transition_table import SceneTransition
from app.db.tables import Campaign, Character, Entity, Scene, SceneThesis
from app.services.action_sequence_executor import ActionSequenceExecutor
from app.services.active_canon_replay import ActiveCanonReplayService
from app.services.scene_bridge_service import SceneBridgeService
from app.services.scene_lifecycle import SceneLifecycleService


class TurnUndoService:
    """Undo the latest user/assistant pair and every structured boundary it caused."""

    def __init__(self, session: AsyncSession):
        self._session = session
        self._turns = TurnRepository(session)
        self._scenes = SceneRepository(session)
        self._bridges = SceneBridgeService(session)

    async def undo_last_pair(self, campaign_id: UUID) -> bool:
        pair = await self._turns.get_latest_undoable_pair(campaign_id)
        if pair is None:
            return False
        user_turn, assistant_turn = pair

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

        if not await self._turns.undo_pair(
            campaign_id,
            user_turn.id,
            assistant_turn.id,
        ):
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
            await self._reconcile_derived_state(campaign_id, assistant_turn.id)
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
            await self._bridges.mark_status(UUID(transition.id), "undone")

        await self._reconcile_derived_state(campaign_id, assistant_turn.id)
        await self._session.flush()
        return True

    async def _reconcile_derived_state(
        self,
        campaign_id: UUID,
        assistant_turn_id: UUID,
    ) -> None:
        """Project the world from still-active turns after the pair becomes undone.

        This deliberately happens after structural scene/action compensation: Turn status decides
        which extracted canon may survive, while scene/action executors remain the authority for
        the current physical boundary.
        """
        await ActiveCanonReplayService(self._session).replay(campaign_id)
        await self._remove_turn_introductions(campaign_id, assistant_turn_id)
        # Curator-created working-memory rows from the undone turn must not remain active.
        # Existing theses changed by a curator are not reconstructed here; unlike durable canon,
        # they are short-lived scene working memory and will be reconciled on the next active turn.
        await self._session.execute(
            delete(SceneThesis).where(
                SceneThesis.source_turn_id == str(assistant_turn_id),
            )
        )

    async def _remove_turn_introductions(
        self,
        campaign_id: UUID,
        assistant_turn_id: UUID,
    ) -> None:
        """Remove characters whose very first existence was created by the undone turn."""
        rows = (
            await self._session.execute(
                select(Entity).where(
                    Entity.campaign_id == str(campaign_id),
                    Entity.entity_type == "character",
                )
            )
        ).scalars().all()
        turn_key = str(assistant_turn_id)
        for entity in rows:
            try:
                fields = json.loads(entity.custom_fields or "{}")
            except (json.JSONDecodeError, TypeError):
                fields = {}
            if not isinstance(fields, dict):
                continue
            planned = fields.get("introduction_turn_id") == turn_key
            extracted = (
                entity.provenance == "narrator_extracted"
                and fields.get("source_turn_id") == turn_key
            )
            if planned or extracted:
                await self._session.delete(entity)
        await self._session.flush()

    async def _restore_scene_participant_locations(self, scene_id: UUID) -> None:
        location_id = await self._scenes.get_location_id(scene_id)
        if not location_id:
            return
        for participant_id in await self._scenes.get_participants(scene_id):
            character = await self._session.get(Character, str(participant_id))
            if character:
                character.current_location_id = str(location_id)
        await self._session.flush()
