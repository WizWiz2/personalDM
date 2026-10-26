from __future__ import annotations

import json
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.provider_config_repo import ProviderConfigRepository
from app.models.proposed_change import ChangeType, ProposedChangeCreate
from app.models.turn import ChatMessage
from app.providers.llm_provider import LLMProvider, LLMProviderError
from app.services.item_state_authority import load_item_states
from app.services.role_model_router import ModelRole, RoleModelRouter


class ItemFactDomain(BaseModel):
    proposal_index: int = Field(ge=0)
    domain: Literal["custody_or_placement", "disclosed_content", "physical_property", "other"]


class ItemFactDomains(BaseModel):
    decisions: list[ItemFactDomain] = Field(max_length=32)


async def guard_item_facts(
    session: AsyncSession, campaign_id: UUID, proposals: list[ProposedChangeCreate], *, router=None,
) -> list[ProposedChangeCreate]:
    """Physical item fluents belong to executors, never to a parallel prose fact slot.

    Meaning is classified by the control model. Exact entity references select the
    candidates; a missing/invalid classification leaves those facts unverified.
    Descriptions and provenance remain usable. Executed document text has one owner.
    """
    if not any(proposal.change_type == ChangeType.FACT for proposal in proposals):
        return proposals
    states = await load_item_states(session, campaign_id)
    names = {state.name.casefold(): str(state.item_id) for state in states}
    ids = set(names.values())
    candidates = {
        index: proposal.payload for index, proposal in enumerate(proposals)
        if proposal.change_type == ChangeType.FACT and (
            str(proposal.payload.get("subject_entity_id") or "") in ids
            or str(proposal.payload.get("subject") or "").strip().casefold() in names
            or str(proposal.payload.get("subject") or "") in ids
        )
    }
    if not candidates:
        return proposals
    router = router or RoleModelRouter(ProviderConfigRepository(session))
    classifications = {}
    try:
        selection = await router.resolve(campaign_id, ModelRole.SCRIBE)
        if selection:
            result = await router.generate_json(
                LLMProvider(), selection,
                [ChatMessage(role="system", content=(
                    "Classify each supplied FACT proposal exactly once, without rewriting it. "
                    "domain=custody_or_placement for current holding, possession, storage, delivery, "
                    "handoff success, location or exact physical position of a registered item. "
                    "These mutable slots are executor-owned even if the text matches current reality. "
                    "domain=disclosed_content for the actual written text, signature or instructions "
                    "of an item whose revealed_content is already supplied. Its disclosure is "
                    "execution-owned too; do not create a second independent content fact. "
                    "domain=physical_property for a current physical condition already represented "
                    "in item_states.properties. That fluent is owned by its executed source too. "
                    "domain=other for appearance, historical origin, purpose, independent evidence "
                    "of authorship, or attributes not already covered by the supplied disclosure. "
                    "Learning who wrote a document does not rewrite its physical text or signature. "
                    "Do not classify by vocabulary; interpret the full proposition."
                )), ChatMessage(role="user", content=json.dumps(
                    {"proposals": candidates, "items": [state.model_dump(mode="json") for state in states]}, ensure_ascii=False,
                ))],
                temperature=0.0, max_tokens=800, response_model=ItemFactDomains,
            )
            decisions = ItemFactDomains.model_validate(result).decisions
            indices = [decision.proposal_index for decision in decisions]
            if len(indices) == len(candidates) and set(indices) == set(candidates):
                classifications = {decision.proposal_index: decision.domain for decision in decisions}
    except (LLMProviderError, ValueError, TypeError):
        pass
    for index in candidates:
        domain = classifications.get(index, "unverified")
        proposals[index].payload["_item_fact_domain"] = domain
        if domain != "other":
            proposals[index].payload["_validation_error"] = (
                "Item custody/placement must come from inventory state and executed receipts; "
                "a parallel prose fact cannot establish or override it"
                if domain == "custody_or_placement" else
                "Disclosed item content is owned by its executed source, not by a prose fact"
                if domain == "disclosed_content" else "Physical item properties belong to executed source effects"
                if domain == "physical_property" else "Item fact domain could not be verified"
            )
    return proposals
