from __future__ import annotations

import json
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import Turn
from app.services.truth_engine_residual import SemanticResidualExtractor
from app.services.truth_engine_turn_context import SemanticTurnContextReader


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
        context_reader: SemanticTurnContextReader | None = None,
    ):
        self._session = session
        self._extractor = extractor or SemanticResidualExtractor(session)
        self._context_reader = context_reader or SemanticTurnContextReader(session)

    async def capture(self, assistant_turn_id: UUID) -> bool:
        context = await self._context_reader.load_active(assistant_turn_id)
        if context is None:
            return False

        envelope = await self._extractor.extract(
            context.campaign_id,
            user_content=context.user_content,
            assistant_content=context.assistant_content,
            structured_receipts=list(context.structured_receipts),
        )

        # End the long LLM read transaction and re-check source activity. An undo that completed
        # while shadow extraction was running must win before any diagnostic write is persisted.
        await self._session.rollback()
        current = await self._context_reader.load_active(assistant_turn_id)
        if current is None or current.user_turn_id != context.user_turn_id:
            return False

        assistant = await self._session.get(Turn, str(assistant_turn_id))
        if assistant is None:
            return False
        snapshot = self._snapshot_dict(assistant.context_snapshot)
        snapshot[self.SNAPSHOT_KEY] = {
            "version": 1,
            "mode": "read_only",
            "source_user_turn_id": str(current.user_turn_id),
            "receipt_count": len(context.structured_receipts),
            "structured_receipts": list(context.structured_receipts),
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
