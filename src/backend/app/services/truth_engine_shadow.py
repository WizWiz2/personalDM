from __future__ import annotations

import json
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import Event, Turn
from app.db.truth_engine_table import TruthEventRecord
from app.services.truth_engine_residual import SemanticResidualExtractor


class SemanticResidualShadowService:
    """Capture TE2 residual observations without mutating canonical world state.

    Shadow mode is deliberately read-only with respect to TE2 events, entities, fluents and
    relations. It records the extractor output in the assistant turn's diagnostic snapshot so live
    suites can compare the new semantic boundary with the legacy Scribe before writer ownership
    changes.
    """

    SNAPSHOT_KEY = "te2_semantic_shadow"

    def __init__(
        self,
        session: AsyncSession,
        *,
        extractor: SemanticResidualExtractor | None = None,
    ):
        self._session = session
        self._extractor = extractor or SemanticResidualExtractor(session)

    async def capture(self, assistant_turn_id: UUID) -> bool:
        assistant = await self._session.get(Turn, str(assistant_turn_id))
        if (
            assistant is None
            or assistant.role != "assistant"
            or assistant.status != "active"
            or not assistant.parent_turn_id
        ):
            return False
        user_turn = await self._session.get(Turn, assistant.parent_turn_id)
        if user_turn is None or user_turn.status != "active":
            return False

        campaign_id = UUID(assistant.campaign_id)
        receipts = await self._structured_receipts(UUID(user_turn.id))
        envelope = await self._extractor.extract(
            campaign_id,
            user_content=user_turn.content,
            assistant_content=assistant.content,
            structured_receipts=receipts,
        )

        # End the long LLM read transaction and re-check source activity. An undo that completed
        # while shadow extraction was running must win before any diagnostic write is persisted.
        await self._session.rollback()
        assistant = await self._session.get(Turn, str(assistant_turn_id))
        if (
            assistant is None
            or assistant.status != "active"
            or not assistant.parent_turn_id
        ):
            return False
        user_turn = await self._session.get(Turn, assistant.parent_turn_id)
        if user_turn is None or user_turn.status != "active":
            return False

        snapshot = self._snapshot_dict(assistant.context_snapshot)
        snapshot[self.SNAPSHOT_KEY] = {
            "version": 1,
            "mode": "read_only",
            "source_user_turn_id": user_turn.id,
            "receipt_count": len(receipts),
            "structured_receipts": receipts,
            "residual": envelope.model_dump(mode="json"),
            "counts": {
                "entities": len(envelope.entities),
                "fluents": len(envelope.fluents),
                "relations": len(envelope.relations),
            },
        }
        assistant.context_snapshot = json.dumps(
            snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        await self._session.flush()
        return True

    async def _structured_receipts(self, source_turn_id: UUID) -> list[dict]:
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

    @staticmethod
    def _snapshot_dict(raw: str | dict | None) -> dict:
        if isinstance(raw, dict):
            return dict(raw)
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
        return value if isinstance(value, dict) else {}


__all__ = ["SemanticResidualShadowService"]
