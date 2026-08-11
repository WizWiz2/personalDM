import json
from uuid import UUID

from sqlalchemy import desc, func, literal_column, select

from app.db.repositories.base import BaseRepository
from app.db.tables import Campaign, Scene, Turn
from app.models.turn import ChatMessage, TurnCreate, TurnRead


NARRATIVE_ROLES = ("user", "assistant", "system")
META_ROLES = ("meta_user", "meta_assistant")


class TurnRepository(BaseRepository):
    async def _resolve_scene_id(
        self,
        campaign_id: UUID,
        data: TurnCreate,
    ) -> str | None:
        """Bind narrative turns to scene state and isolate meta dialogue.

        Meta turns are deliberately scene-less. They may inspect the current scene in
        their compiled context, but persisting them must never imply movement, presence
        or a scene transition.
        """
        campaign_key = str(campaign_id)

        if data.role in META_ROLES:
            if data.role == "meta_assistant":
                if not data.parent_turn_id:
                    raise ValueError("Meta assistant turn requires a parent meta user turn")
                parent = await self._session.get(Turn, str(data.parent_turn_id))
                if (
                    not parent
                    or parent.campaign_id != campaign_key
                    or parent.role != "meta_user"
                ):
                    raise ValueError(
                        "Meta assistant parent must be a meta user turn in the same campaign"
                    )
            return None

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
                    transition = (data.context_snapshot or {}).get(
                        "scene_transition"
                    ) or {}
                    authorized = (
                        transition.get("status")
                        in {"prepared", "applied", "reused"}
                        and str(transition.get("target_scene_id") or "") == resolved
                        and str(transition.get("source_scene_id") or "")
                        == str(parent.scene_id or "")
                    )
                    if not authorized:
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
        channel: str = "narrative",
    ) -> list[TurnRead]:
        query = select(Turn).where(Turn.campaign_id == str(campaign_id))
        if channel == "narrative":
            query = query.where(Turn.role.in_(NARRATIVE_ROLES))
        elif channel == "meta":
            query = query.where(Turn.role.in_(META_ROLES))
        elif channel != "all":
            raise ValueError("Turn history channel must be narrative, meta or all")
        if active_only:
            query = query.where(Turn.status == "active")
        query = query.order_by(Turn.created_at.desc()).limit(limit)

        result = await self._session.execute(query)
        turns = result.scalars().all()
        return [TurnRead.model_validate(turn) for turn in reversed(turns)]

    async def get_meta_history(
        self,
        campaign_id: UUID,
        limit: int = 10,
    ) -> list[TurnRead]:
        return await self.get_history(
            campaign_id,
            limit=limit,
            active_only=True,
            channel="meta",
        )

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
                Turn.role.in_(NARRATIVE_ROLES),
            )
            .order_by(desc(Turn.created_at))
            .limit(max_turns)
        )
        turns = result.scalars().all()
        return [
            ChatMessage(role=turn.role, content=turn.content)
            for turn in reversed(turns)
        ]

    async def get_latest_undoable_pair(
        self,
        campaign_id: UUID,
    ) -> tuple[TurnRead, TurnRead] | None:
        """Resolve the latest narrative pair from durable parent provenance.

        Undo must not depend on the user and assistant rows being adjacent in history. A
        delayed assistant row from an older turn, meta traffic, or another historical row
        may sit between them. The newest active narrative row must still be an assistant,
        and its explicit parent must be an active user turn in the same campaign.
        """
        campaign_key = str(campaign_id)
        newest = (
            await self._session.execute(
                select(Turn)
                .where(
                    Turn.campaign_id == campaign_key,
                    Turn.status == "active",
                    Turn.role.in_(("user", "assistant")),
                )
                .order_by(
                    desc(Turn.created_at),
                    literal_column("turns.rowid").desc(),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if newest is None or newest.role != "assistant" or not newest.parent_turn_id:
            return None

        parent = (
            await self._session.execute(
                select(Turn).where(
                    Turn.id == newest.parent_turn_id,
                    Turn.campaign_id == campaign_key,
                    Turn.status == "active",
                    Turn.role == "user",
                )
            )
        ).scalar_one_or_none()
        if parent is None:
            return None
        return TurnRead.model_validate(parent), TurnRead.model_validate(newest)

    async def undo_pair(
        self,
        campaign_id: UUID,
        user_turn_id: UUID,
        assistant_turn_id: UUID,
    ) -> bool:
        """Mark one explicitly linked active narrative pair undone."""
        rows = (
            await self._session.execute(
                select(Turn).where(
                    Turn.campaign_id == str(campaign_id),
                    Turn.status == "active",
                    Turn.id.in_((str(user_turn_id), str(assistant_turn_id))),
                )
            )
        ).scalars().all()
        by_id = {row.id: row for row in rows}
        user_turn = by_id.get(str(user_turn_id))
        assistant_turn = by_id.get(str(assistant_turn_id))
        if (
            user_turn is None
            or assistant_turn is None
            or user_turn.role != "user"
            or assistant_turn.role != "assistant"
            or assistant_turn.parent_turn_id != user_turn.id
        ):
            return False

        assistant_turn.status = "undone"
        user_turn.status = "undone"
        await self._session.flush()
        return True

    async def undo_last_pair(self, campaign_id: UUID) -> bool:
        pair = await self.get_latest_undoable_pair(campaign_id)
        if pair is None:
            return False
        user_turn, assistant_turn = pair
        return await self.undo_pair(campaign_id, user_turn.id, assistant_turn.id)

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
