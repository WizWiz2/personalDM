from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import Event, Turn
from app.db.truth_engine_table import TruthEventRecord


@dataclass(frozen=True)
class SemanticTurnContext:
    campaign_id: UUID
    user_turn_id: UUID
    assistant_turn_id: UUID
    scene_id: UUID | None
    acting_character_id: UUID | None
    user_content: str
    assistant_content: str
    structured_receipts: tuple[dict, ...]


class SemanticTurnContextReader:
    """Load one active completed turn pair for TE2 semantic work.

    The reader is intentionally storage-only: it does not interpret prose and does not decide
    semantic ownership. Both read-only shadowing and the future writer path use the same source-pair
    and executor-receipt boundary so evaluation cannot drift from production cutover behavior.
    """

    def __init__(self, session: AsyncSession):
        self._session = session

    async def load_active(self, assistant_turn_id: UUID) -> SemanticTurnContext | None:
        assistant = await self._session.get(Turn, str(assistant_turn_id))
        if (
            assistant is None
            or assistant.role != "assistant"
            or assistant.status != "active"
            or not assistant.parent_turn_id
        ):
            return None
        user_turn = await self._session.get(Turn, assistant.parent_turn_id)
        if user_turn is None or user_turn.role != "user" or user_turn.status != "active":
            return None

        user_turn_id = UUID(user_turn.id)
        return SemanticTurnContext(
            campaign_id=UUID(assistant.campaign_id),
            user_turn_id=user_turn_id,
            assistant_turn_id=UUID(assistant.id),
            scene_id=UUID(assistant.scene_id) if assistant.scene_id else None,
            acting_character_id=(
                UUID(assistant.acting_character_id) if assistant.acting_character_id else None
            ),
            user_content=user_turn.content,
            assistant_content=assistant.content,
            structured_receipts=tuple(await self.structured_receipts(user_turn_id)),
        )

    async def pair_is_active(
        self,
        assistant_turn_id: UUID,
        expected_user_turn_id: UUID,
    ) -> bool:
        """Check the source pair inside the caller's current transaction.

        Writer mode calls this only after acquiring SQLite's short write lock. Keeping this check
        free of receipt loading makes the guarded critical section tiny and deterministic.
        """
        row = (
            await self._session.execute(
                select(Turn.status, Turn.parent_turn_id).where(
                    Turn.id == str(assistant_turn_id),
                    Turn.role == "assistant",
                )
            )
        ).one_or_none()
        if (
            row is None
            or row.status != "active"
            or row.parent_turn_id != str(expected_user_turn_id)
        ):
            return False
        parent_status = (
            await self._session.execute(
                select(Turn.status).where(
                    Turn.id == str(expected_user_turn_id),
                    Turn.role == "user",
                )
            )
        ).scalar_one_or_none()
        return parent_status == "active"

    async def structured_receipts(self, source_turn_id: UUID) -> list[dict]:
        rows = list(
            (
                await self._session.execute(
                    select(TruthEventRecord, Event)
                    .join(Event, Event.id == TruthEventRecord.event_id)
                    .where(
                        TruthEventRecord.source_turn_id == str(source_turn_id),
                        TruthEventRecord.source_kind == "executor_receipt",
                        TruthEventRecord.status == "active",
                    )
                    .order_by(TruthEventRecord.sequence, TruthEventRecord.event_id)
                )
            ).all()
        )
        receipts: list[dict] = []
        for record, event in rows:
            try:
                payload = json.loads(record.payload_json or "{}")
            except (json.JSONDecodeError, TypeError):
                payload = {}
            receipts.append(
                {
                    "event_id": record.event_id,
                    "event_type": event.event_type,
                    "description": event.description,
                    "payload": payload if isinstance(payload, dict) else {},
                }
            )
        return receipts


__all__ = ["SemanticTurnContext", "SemanticTurnContextReader"]
