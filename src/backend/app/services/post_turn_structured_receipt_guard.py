from __future__ import annotations

import json
import re
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import or_, select

from app.config import settings
from app.db.repositories.proposed_change_repo import ProposedChangeRepository
from app.db.repositories.provider_config_repo import ProviderConfigRepository
from app.db.tables import PostTurnJob, RelationshipAssertion
from app.models.proposed_change import (
    ChangeType,
    ProposalAction,
    ProposedChangeCreate,
)
from app.models.turn import ChatMessage
from app.providers.llm_provider import LLMProvider
from app.services.canon_applier import CanonApplier
from app.services.post_turn_processor import PostTurnProcessor
from app.services.role_model_router import ModelRole, RoleModelRouter

_INSTALLED = False


class RelationshipReceiptDecision(BaseModel):
    verdict: Literal["no_change", "retract"] = "no_change"
    retract_ids: list[UUID] = Field(default_factory=list)
    reason: str = ""


def _explicit_item_debt_fulfillments(receipt: dict, relationships) -> set[UUID]:
    """Return debts whose own text names the item in a confirmed give receipt.

    The receipt is already an authoritative executor result.  For a debt, an exact
    item match is enough to close only when the current assertion itself names that
    item; generic obligations and unrelated transfers remain delegated to the
    semantic reconciler below.
    """
    if receipt.get("operation") != "give":
        return set()
    item_text = str(receipt.get("item_name") or "")
    from_id = str(receipt.get("from_character_id") or "")
    to_id = str(receipt.get("to_character_id") or "")
    if not item_text or not from_id or not to_id:
        return set()
    item_tokens = {
        token.casefold()
        for token in re.findall(r"[\w-]{3,}", item_text, flags=re.UNICODE)
    }
    if not item_tokens:
        return set()
    result: set[UUID] = set()
    for row in relationships:
        if str(row.relation_type or "").casefold() != "debt":
            continue
        if {str(row.subject_id), str(row.object_id)} != {from_id, to_id}:
            continue
        description_tokens = {
            token.casefold()
            for token in re.findall(r"[\w-]{3,}", str(row.description or ""), flags=re.UNICODE)
        }
        overlap = len(item_tokens & description_tokens)
        if overlap >= min(2, len(item_tokens)):
            result.add(UUID(str(row.id)))
    return result


def _snapshot_dict(turn) -> dict:
    raw = getattr(turn, "context_snapshot", None)
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _executed_steps(assistant) -> list[dict]:
    authority = _snapshot_dict(assistant).get("turn_authority") or {}
    sequence = authority.get("action_sequence") if isinstance(authority, dict) else None
    steps = sequence.get("steps") if isinstance(sequence, dict) else None
    return [step for step in (steps or []) if isinstance(step, dict)]


def _player_id(assistant) -> UUID | None:
    authority = _snapshot_dict(assistant).get("turn_authority") or {}
    raw = authority.get("player_character_id") if isinstance(authority, dict) else None
    try:
        return UUID(str(raw)) if raw else None
    except (TypeError, ValueError):
        return None


async def _relationship_candidates(
    processor: PostTurnProcessor,
    campaign_id: UUID,
    player_id: UUID,
    target_id: UUID,
):
    return (
        await processor._session.execute(
            select(RelationshipAssertion).where(
                RelationshipAssertion.campaign_id == str(campaign_id),
                RelationshipAssertion.is_current.is_(True),
                or_(
                    (
                        (RelationshipAssertion.subject_id == str(player_id))
                        & (RelationshipAssertion.object_id == str(target_id))
                    ),
                    (
                        (RelationshipAssertion.subject_id == str(target_id))
                        & (RelationshipAssertion.object_id == str(player_id))
                    ),
                ),
            )
        )
    ).scalars().all()


async def _relationship_decision(
    processor: PostTurnProcessor,
    campaign_id: UUID,
    receipt: dict,
    relationships,
    user_content: str,
    assistant_content: str,
) -> RelationshipReceiptDecision:
    router = RoleModelRouter(ProviderConfigRepository(processor._session))
    selection = await router.resolve(campaign_id, ModelRole.SCRIBE)
    if selection is None:
        return RelationshipReceiptDecision()

    cards = [
        {
            "id": row.id,
            "subject_id": row.subject_id,
            "object_id": row.object_id,
            "relation_type": row.relation_type,
            "description": row.description,
            "reason": row.reason,
            "intensity": row.intensity,
        }
        for row in relationships
    ]
    prompt = """
You are a narrow relationship receipt reconciler for an RPG truth engine.
Return JSON only. You may select ONLY relationship IDs from CURRENT_RELATIONSHIPS.
A structured receipt is machine-confirmed: the listed action definitely happened.

Retract a current relationship only when that completed receipt clearly fulfills, ends, or makes
false the relationship's own explicit continuing condition. Example: a debt whose description says
it remains until a specific item is returned is retracted when the structured receipt confirms that
exact return to the creditor.

Do NOT retract friendship, trust, hostility, kinship, employment, generic obligation, or any relation
merely because an item was transferred. Do not infer hidden meaning. When uncertain use no_change.
The player's prose can clarify intent, but it cannot override the machine-confirmed receipt.

Schema:
{"verdict":"no_change|retract","retract_ids":["uuid"],"reason":"brief Russian reason"}
"""
    data = await router.generate_json(
        LLMProvider(),
        selection,
        [
            ChatMessage(role="system", content=prompt),
            ChatMessage(
                role="user",
                content=(
                    "STRUCTURED_RECEIPT:\n"
                    + json.dumps(receipt, ensure_ascii=False)
                    + "\n\nCURRENT_RELATIONSHIPS:\n"
                    + json.dumps(cards, ensure_ascii=False)
                    + "\n\nPLAYER_INPUT:\n"
                    + user_content
                    + "\n\nPUBLISHED_RESULT:\n"
                    + assistant_content
                ),
            ),
        ],
        max_tokens=450,
        temperature=0.0,
        response_model=RelationshipReceiptDecision,
    )
    # Provider adapters return JSON-compatible mappings even when a response_model
    # was supplied. Re-validate at this boundary so callers always receive the typed
    # decision contract and cannot accidentally treat a dict as an object.
    return RelationshipReceiptDecision.model_validate(data)


async def _ensure_relationship_receipts(
    processor: PostTurnProcessor,
    campaign_id: UUID,
    assistant,
    user_turn,
) -> int:
    """Temporary legacy bridge for relationships not yet migrated to TE2 semantic relations."""

    # Once TE2 owns objective relations this bridge is not a compatibility projection: it would be
    # a second semantic writer. Refuse to run it even when called directly by an old wrapper/test.
    if settings.TE2_SEMANTIC_MODE == "writer":
        return 0

    player_id = _player_id(assistant)
    if player_id is None:
        return 0
    gives = [
        step
        for step in _executed_steps(assistant)
        if step.get("status") == "completed"
        and step.get("action_type") == "inventory"
        and step.get("item_operation") == "give"
        and step.get("item_result_owner_id")
    ]
    if not gives:
        return 0

    proposal_repo = ProposedChangeRepository(processor._session)
    existing = await proposal_repo.get_for_turn(assistant.id)
    external_resolution = await processor._uses_external_proposal_resolution(user_turn.id)
    applied = 0

    for step in gives:
        try:
            target_id = UUID(str(step["item_result_owner_id"]))
        except (TypeError, ValueError):
            continue
        relationships = await _relationship_candidates(
            processor,
            campaign_id,
            player_id,
            target_id,
        )
        if not relationships:
            continue

        receipt = {
            "operation": "give",
            "item_id": step.get("item_id"),
            "item_name": step.get("item_name"),
            "from_character_id": str(player_id),
            "to_character_id": str(target_id),
            "observable_outcome": step.get("observable_outcome"),
        }
        decision = await _relationship_decision(
            processor,
            campaign_id,
            receipt,
            relationships,
            user_turn.content,
            assistant.content,
        )
        deterministic_retract_ids = _explicit_item_debt_fulfillments(receipt, relationships)
        retract_ids = set(decision.retract_ids) if decision.verdict == "retract" else set()
        retract_ids.update(deterministic_retract_ids)
        if not retract_ids:
            continue

        by_id = {UUID(row.id): row for row in relationships}
        for relationship_id in retract_ids:
            row = by_id.get(relationship_id)
            if row is None:
                continue
            duplicate = next(
                (
                    proposal
                    for proposal in existing
                    if proposal.change_type == ChangeType.RELATIONSHIP.value
                    and str((proposal.payload or {}).get("_retract_relationship_id"))
                    == str(relationship_id)
                ),
                None,
            )
            if duplicate is not None:
                continue

            payload = {
                "subject_id": row.subject_id,
                "object_id": row.object_id,
                "relation_type": row.relation_type,
                "description": row.description,
                "reason": decision.reason or row.reason,
                "intensity": row.intensity,
                "visibility": row.visibility,
                "operation": "retract",
                "cardinality": "single",
                "_structured_receipt": receipt,
                "_retract_relationship_id": str(relationship_id),
            }
            created = await proposal_repo.create_batch(
                assistant.id,
                [ProposedChangeCreate(change_type=ChangeType.RELATIONSHIP, payload=payload)],
            )
            proposal = created[0]
            existing.append(proposal)
            if external_resolution:
                continue
            await CanonApplier(processor._session).apply(
                campaign_id,
                ChangeType.RELATIONSHIP,
                payload,
                assistant.id,
            )
            await proposal_repo.resolve(
                proposal.id,
                ProposalAction(status="accepted"),
            )
            applied += 1
    return applied


async def reconcile_structured_receipts(
    processor: PostTurnProcessor,
    job_id: UUID,
) -> None:
    """Run only legacy semantic receipt reconciliation not yet owned by TE2.

    Movement and inventory physical state are intentionally absent here: applied executor receipts
    are already canonicalized synchronously by StructuredReceiptEventCompiler. Keeping another
    post-turn writer would create two competing sources of truth.
    """

    if settings.TE2_SEMANTIC_MODE == "writer":
        return
    row = await processor._session.get(PostTurnJob, str(job_id))
    if row is None or row.status != "completed" or row.job_type != "memory_scribe":
        return
    assistant = await processor._turns.get_by_id(UUID(row.assistant_turn_id))
    if (
        assistant is None
        or assistant.status != "active"
        or not assistant.parent_turn_id
        or not processor._authority_managed(assistant)
    ):
        return
    user_turn = await processor._turns.get_by_id(assistant.parent_turn_id)
    if user_turn is None or user_turn.status != "active":
        return

    campaign_id = UUID(row.campaign_id)
    await _ensure_relationship_receipts(
        processor,
        campaign_id,
        assistant,
        user_turn,
    )
    await processor._session.commit()


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    original_process_job = PostTurnProcessor.process_job

    async def receipt_aware_process_job(self, job_id, *, already_claimed=False):
        await original_process_job(
            self,
            job_id,
            already_claimed=already_claimed,
        )
        try:
            await reconcile_structured_receipts(self, job_id)
        except Exception as exc:
            await self._session.rollback()
            row = await self._session.get(PostTurnJob, str(job_id))
            if row is not None:
                row.status = "failed"
                row.error = f"structured receipt reconciliation failed: {exc}"[:4000]
                row.locked_at = None
                await self._session.commit()
            raise

    PostTurnProcessor.process_job = receipt_aware_process_job
    _INSTALLED = True


__all__ = [
    "RelationshipReceiptDecision",
    "_executed_steps",
    "_ensure_relationship_receipts",
    "install",
    "reconcile_structured_receipts",
]
