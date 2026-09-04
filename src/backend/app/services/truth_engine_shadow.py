from __future__ import annotations

import json
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import Turn
from app.models.truth_engine_residual import ResidualSanitizationAudit
from app.services.truth_engine_residual import SemanticResidualExtractor
from app.services.truth_engine_residual_gate import SemanticResidualDispositionGate
from app.services.truth_engine_turn_context import SemanticTurnContextReader


class SemanticResidualShadowService:
    """Capture TE2 residual observations without mutating canonical world state.

    Shadow records the recall-oriented extractor output, deterministic sanitation diagnostics, and the
    bounded disposition result. This keeps extractor mistakes visible while exposing the exact
    objective residual that writer mode would receive after the truth-classification gate.
    """

    SNAPSHOT_KEY = "te2_semantic_shadow"

    def __init__(
        self,
        session: AsyncSession,
        *,
        extractor: SemanticResidualExtractor | None = None,
        classifier: SemanticResidualDispositionGate | None = None,
        context_reader: SemanticTurnContextReader | None = None,
    ):
        self._session = session
        self._extractor = extractor or SemanticResidualExtractor(session)
        self._classifier = classifier or SemanticResidualDispositionGate(session)
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
        sanitization = getattr(
            self._extractor,
            "last_sanitization_audit",
            ResidualSanitizationAudit(),
        )
        classification = await self._classifier.classify(
            context.campaign_id,
            envelope=envelope,
            user_content=context.user_content,
            assistant_content=context.assistant_content,
            structured_receipts=list(context.structured_receipts),
        )

        # End the long LLM read transaction and re-check source activity. An undo that completed
        # while either shadow model call was running must win before diagnostic metadata is persisted.
        await self._session.rollback()
        current = await self._context_reader.load_active(assistant_turn_id)
        if current is None or current.user_turn_id != context.user_turn_id:
            return False

        assistant = await self._session.get(Turn, str(assistant_turn_id))
        if assistant is None:
            return False
        snapshot = self._snapshot_dict(assistant.context_snapshot)
        objective = classification.objective
        dispositions = [
            decision.model_dump(mode="json") for decision in classification.decisions
        ]
        snapshot[self.SNAPSHOT_KEY] = {
            "version": 3,
            "mode": "read_only",
            "source_user_turn_id": str(current.user_turn_id),
            "receipt_count": len(context.structured_receipts),
            "structured_receipts": list(context.structured_receipts),
            "sanitization": sanitization.model_dump(mode="json"),
            "residual": envelope.model_dump(mode="json"),
            "dispositions": dispositions,
            "objective_residual": objective.model_dump(mode="json"),
            "counts": {
                "entities": len(envelope.entities),
                "fluents": len(envelope.fluents),
                "relations": len(envelope.relations),
                "objective_entities": len(objective.entities),
                "objective_fluents": len(objective.fluents),
                "objective_relations": len(objective.relations),
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
