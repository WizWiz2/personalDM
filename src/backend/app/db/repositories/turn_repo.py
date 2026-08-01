import json
from uuid import UUID

from sqlalchemy import desc, func, select

from app.db.repositories.base import BaseRepository
from app.db.scene_transition_table import SceneTransition
from app.db.tables import Campaign, Scene, Turn
from app.models.turn import ChatMessage, TurnCreate, TurnRead


class TurnRepository(BaseRepository):
    async def _resolve_scene_id(
        self,
        campaign_id: UUID,
        data: TurnCreate,
    ) -> str | None:
        """Bind persisted turns to authoritative structured scene state.

        A user turn follows campaign.current_scene_id even when a stale client sends
        the previous scene. Assistant turns normally inherit their parent user scene.
        An explicit different target is accepted only when an applied structured scene
        transition links that parent user turn to the target scene.
        """
        campaign_key = str(campaign_id)
        resolved = str(data.scene_id) if data.scene_id else None

        if data.role == "user":
            campaign = await self._session.get(Campaign, campaign_key)
            if campaign and campaign.current_scene_id:
                resolved = campaign.current_scene_id
        elif data.role == "assistant" and data.parent_turn_id:
            parent = await self._session.get(Turn, str(data.parent_turn_id))
            if parent and parent.campaign_id == campaign_key:
                if not resolved:
                    resolved = parent.scene_id
                elif resolved != parent.scene_id:
                    transition = (
                        await self._session.execute(
                            select(SceneTransition.id).where(
                                SceneTransition.campaign_id == campaign_key,
                                SceneTransition.trigger_turn_id == parent.id,
                                SceneTransition.target_scene_id == resolved,
                                SceneTransition.status == "applied",
                            )
                        )
                    ).scalar_one_or_none()
                    if not transition:
                        raise ValueError(
                            "Assistant turn may change scenes only through an applied transition"
                        )

        if resolved:
            scene_campaign_id = (
                await self._session.execute(
                    select(Scene.campaign_id).where(Scene.id == resolved)
                )
            ).scalar_one_or_none()
            if scene_campaign_id != campaign_key:
                raise ValueError(
                    "Turn scene must belong to the same campaign "
                    f"(scene_id={resolved}, campaign_id={campaign_key})"
                )
        return resolved

    async def create(self, campaign_id: UUID, data: TurnCreate) -> TurnRead:
        context_str = (
            json.dumps(data.context_snapshot)
            if data.context_snapshot is not None
            else None
        )
        db_turn = Turn(
            campaign_id=str(campaign_id),
            scene_id=await self._resolve_scene_id(campaign_id, data),
            acting_character_id=(
                str(data.acting_character_id) if data.acting_character_id else None
            ),
            role=data.role,
            content=data.content,
            parent_turn_id=str(data.parent_turn_id) if data.parent_turn_id else None,
            status="active",
            model_name=data.model_name,
            context_snapshot=context_str,
            token_count=data.token_count,
        )
        self._session.add(db_turn)
        await self._session.flush()
        return TurnRead.model_validate(db_turn)

    async def get_by_id(self, turn_id: UUID) -> TurnRead | None:
        result = await self._session.execute(
            select(Turn).where(Turn.id == str(turn_id))
        )
        db_turn = result.scalar_one_or_none()
        if not db_turn:
            return None
        return TurnRead.model_validate(db_turn)

    async def get_history(
        self,
        campaign_id: UUID,
        limit: int = 50,
        active_only: bool = True,
    ) -> list[TurnRead]:
        query = select(Turn).where(Turn.campaign_id == str(campaign_id))
        if active_only:
            query = query.where(Turn.status == "active")
        query = query.order_by(Turn.created_at.desc()).limit(limit)

        result = await self._session.execute(query)
        turns = result.scalars().all()
        return [TurnRead.model_validate(turn) for turn in reversed(turns)]

    async def assistant_turn_number_in_scene(self, turn_id: UUID) -> int:
        turn = await self._session.get(Turn, str(turn_id))
        if not turn or turn.role != "assistant" or not turn.scene_id:
            return 0
        result = await self._session.execute(
            select(func.count())
            .select_from(Turn)
            .where(
                Turn.scene_id == turn.scene_id,
                Turn.role == "assistant",
                Turn.status == "active",
                Turn.created_at <= turn.created_at,
            )
        )
        return int(result.scalar_one())

    async def get_sliding_window(
        self,
        campaign_id: UUID,
        max_turns: int,
    ) -> list[ChatMessage]:
        result = await self._session.execute(
            select(Turn)
            .where(
                Turn.campaign_id == str(campaign_id),
                Turn.status == "active",
            )
            .order_by(desc(Turn.created_at))
            .limit(max_turns)
        )
        turns = result.scalars().all()
        return [
            ChatMessage(role=turn.role, content=turn.content)
            for turn in reversed(turns)
        ]

    async def undo_last_pair(self, campaign_id: UUID) -> bool:
        result = await self._session.execute(
            select(Turn)
            .where(
                Turn.campaign_id == str(campaign_id),
                Turn.status == "active",
            )
            .order_by(desc(Turn.created_at))
            .limit(2)
        )
        last_turns = result.scalars().all()
        if len(last_turns) != 2:
            return False

        newest, previous = last_turns
        if newest.role != "assistant" or previous.role != "user":
            return False
        if newest.parent_turn_id != previous.id:
            return False

        newest.status = "undone"
        previous.status = "undone"
        await self._session.flush()
        return True

    async def mark_alternative(self, turn_id: UUID) -> bool:
        result = await self._session.execute(
            select(Turn).where(Turn.id == str(turn_id))
        )
        db_turn = result.scalar_one_or_none()
        if not db_turn:
            return False

        db_turn.status = "alternative"
        await self._session.flush()
        return True

    async def mark_failed(self, turn_id: UUID) -> bool:
        result = await self._session.execute(
            select(Turn).where(Turn.id == str(turn_id))
        )
        db_turn = result.scalar_one_or_none()
        if not db_turn:
            return False

        db_turn.status = "failed"
        await self._session.flush()
        return True
