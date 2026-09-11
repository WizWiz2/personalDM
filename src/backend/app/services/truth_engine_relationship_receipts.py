from __future__ import annotations

import re
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.proposed_change_repo import ProposedChangeRepository
from app.db.tables import Entity, RelationshipAssertion
from app.models.proposed_change import ChangeType, ProposalAction, ProposedChangeCreate
from app.services.canon_applier import CanonApplier

_WORD_RE = re.compile(r"[\w-]{3,}", flags=re.UNICODE)
_PROJECTION_MARKER = "_te2_receipt_debt_projection_id"


def _tokens(value: object) -> set[str]:
    return {
        token.casefold().replace("ё", "е")
        for token in _WORD_RE.findall(str(value or ""))
    }


def _item_matches_debt(item_name: str, description: str) -> bool:
    """Require the current debt itself to identify the transferred item.

    This is deliberately lexical only at the compatibility-projection boundary: the semantic fact
    that the transfer happened comes from the executor receipt, while this matcher only identifies
    which already-existing legacy debt row names that exact item. It never infers a new obligation.
    """
    item_tokens = _tokens(item_name)
    if not item_tokens:
        return False
    overlap = item_tokens & _tokens(description)
    return len(overlap) >= min(2, len(item_tokens))


def _completed_give(receipt: dict) -> dict | None:
    payload = receipt.get("payload") if isinstance(receipt, dict) else None
    if not isinstance(payload, dict):
        return None
    if (
        payload.get("status") != "completed"
        or payload.get("action_type") != "inventory"
        or payload.get("item_operation") != "give"
        or not payload.get("item_id")
        or not payload.get("item_previous_owner_id")
        or not payload.get("item_result_owner_id")
    ):
        return None
    return payload


class StructuredRelationshipReceiptProjector:
    """Project machine-confirmed relationship consequences into legacy read models.

    TE2 executor receipts remain the authority. This service does not inspect player/narrator prose
    and cannot create a relationship from scratch. It only closes an already-current item-backed debt
    when a completed ``give`` receipt proves that the named item moved between the debt participants.

    The replacement is persisted as an accepted ProposedChange and applied through CanonApplier. That
    makes the compatibility projection idempotent and lets ActiveCanonReplay restore the superseded
    relationship correctly after /undo or any later replay.

    The caller owns the transaction and must perform the normal TE2 source-pair activity check before
    entering this projector.
    """

    def __init__(self, session: AsyncSession):
        self._session = session
        self._proposals = ProposedChangeRepository(session)
        self._applier = CanonApplier(session)

    async def project(
        self,
        campaign_id: UUID,
        assistant_turn_id: UUID,
        structured_receipts: tuple[dict, ...] | list[dict],
    ) -> int:
        existing = await self._proposals.get_for_turn(assistant_turn_id)
        existing_markers = {
            str((proposal.payload or {}).get(_PROJECTION_MARKER) or "")
            for proposal in existing
            if proposal.change_type == ChangeType.RELATIONSHIP.value
        }

        applied = 0
        for receipt in structured_receipts:
            payload = _completed_give(receipt)
            if payload is None:
                continue
            try:
                item_id = UUID(str(payload["item_id"]))
                from_id = UUID(str(payload["item_previous_owner_id"]))
                to_id = UUID(str(payload["item_result_owner_id"]))
            except (TypeError, ValueError):
                continue
            if from_id == to_id:
                continue

            item = await self._session.get(Entity, str(item_id))
            if item is None or item.entity_type != "item":
                continue
            item_name = str(item.canonical_name or "").strip()
            if not item_name:
                continue

            debts = list(
                (
                    await self._session.execute(
                        select(RelationshipAssertion).where(
                            RelationshipAssertion.campaign_id == str(campaign_id),
                            RelationshipAssertion.is_current.is_(True),
                            RelationshipAssertion.relation_type == "debt",
                            or_(
                                (
                                    (RelationshipAssertion.subject_id == str(from_id))
                                    & (RelationshipAssertion.object_id == str(to_id))
                                ),
                                (
                                    (RelationshipAssertion.subject_id == str(to_id))
                                    & (RelationshipAssertion.object_id == str(from_id))
                                ),
                            ),
                        )
                    )
                ).scalars().all()
            )
            for debt in debts:
                marker = str(debt.id)
                if marker in existing_markers:
                    continue
                if not _item_matches_debt(item_name, str(debt.description or "")):
                    continue

                projection_payload = {
                    "subject_id": str(debt.subject_id),
                    "object_id": str(debt.object_id),
                    "relation_type": str(debt.relation_type),
                    "description": (
                        f"Обязательство выполнено: предмет «{item_name}» передан адресату; "
                        "долг закрыт."
                    ),
                    "reason": (
                        "Machine-confirmed completed give receipt fulfilled the item-backed debt."
                    ),
                    "intensity": 0.0,
                    "visibility": str(debt.visibility or "public"),
                    "operation": "revise",
                    "cardinality": "single",
                    _PROJECTION_MARKER: marker,
                    "_structured_receipt_event_id": str(receipt.get("event_id") or ""),
                    "_structured_receipt": {
                        "item_id": str(item_id),
                        "item_name": item_name,
                        "operation": "give",
                        "from_character_id": str(from_id),
                        "to_character_id": str(to_id),
                    },
                }
                created = await self._proposals.create_batch(
                    assistant_turn_id,
                    [
                        ProposedChangeCreate(
                            change_type=ChangeType.RELATIONSHIP,
                            payload=projection_payload,
                        )
                    ],
                )
                proposal = created[0]
                await self._applier.apply(
                    campaign_id,
                    ChangeType.RELATIONSHIP,
                    projection_payload,
                    assistant_turn_id,
                )
                await self._proposals.resolve(
                    proposal.id,
                    ProposalAction(status="accepted"),
                )
                existing_markers.add(marker)
                applied += 1

        await self._session.flush()
        return applied


__all__ = [
    "StructuredRelationshipReceiptProjector",
    "_completed_give",
    "_item_matches_debt",
]
