import json
from uuid import UUID

from sqlalchemy import desc, select

from app.db.repositories.base import BaseRepository
from app.db.tables import Turn
from app.models.turn import ChatMessage, TurnCreate, TurnRead


class TurnRepository(BaseRepository):
    async def create(self, campaign_id: UUID, data: TurnCreate) -> TurnRead:
        context_str = (
            json.dumps(data.context_snapshot)
            if data.context_snapshot is not None
            else None
        )
        db_turn = Turn(
            campaign_id=str(campaign_id),
            scene_id=str(data.scene_id) if data.scene_id else None,
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
