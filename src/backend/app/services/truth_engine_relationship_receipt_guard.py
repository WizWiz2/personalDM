from __future__ import annotations

from app.services.truth_engine_relationship_receipts import (
    StructuredRelationshipReceiptProjector,
)

_INSTALLED = False


def install() -> None:
    """Attach deterministic relationship projection to the TE2 writer transaction boundary.

    The generic legacy receipt reconciler remains disabled in writer mode. This wrapper is narrower:
    it runs only after the TE2 semantic writer accepted an active narrator-managed turn, consumes only
    machine executor receipts, and projects only already-existing item-backed debt resolution.
    """
    global _INSTALLED
    if _INSTALLED:
        return

    from app.services.truth_engine_writer import (
        SemanticResidualWriterService,
        SemanticSourceInactive,
    )

    original_write = SemanticResidualWriterService.write

    async def receipt_projecting_write(self, assistant_turn_id):
        written = await original_write(self, assistant_turn_id)
        if not written:
            return False

        context = await self._context_reader.load_active(assistant_turn_id)  # noqa: SLF001
        if context is None:
            return written

        try:
            # Use the writer's existing short BEGIN IMMEDIATE + active-pair guard. The projection
            # therefore cannot race /undo between source validation and the legacy read-model write.
            await self._begin_guarded_write(context)  # noqa: SLF001
            await StructuredRelationshipReceiptProjector(self._session).project(  # noqa: SLF001
                context.campaign_id,
                context.assistant_turn_id,
                context.structured_receipts,
            )
            await self._session.commit()  # noqa: SLF001
        except SemanticSourceInactive:
            await self._session.rollback()  # noqa: SLF001
        except Exception:
            await self._session.rollback()  # noqa: SLF001
            raise
        return written

    SemanticResidualWriterService.write = receipt_projecting_write
    _INSTALLED = True


__all__ = ["install"]
