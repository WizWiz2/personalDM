from __future__ import annotations

import json
import re
from contextvars import ContextVar
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.config import settings
from app.models.proposed_change import ChangeType, ProposalAction, ProposedChangeCreate
from app.models.turn import ChatMessage
from app.providers.llm_provider import LLMProviderError
from app.services.canon_applier import CanonApplier
from app.services.canon_semantics import CanonEnvelope
from app.services.role_model_router import ModelRole, RoleModelRouter
from app.services.semantic_receipt_context import memory_evidence

_INSTALLED = False
_QUOTE_RE = re.compile(r"«([^»]{2,1600})»|“([^”]{2,1600})”|\"([^\"]{2,1600})\"")
_WORD_RE = re.compile(r"[\w-]+", flags=re.UNICODE)
_PLANNER_CALL_BUDGET = 12
_PLANNER_CALL_STATE: ContextVar[dict[str, int] | None] = ContextVar(
    "pdm_planner_call_budget",
    default=None,
)


class QuoteClaimAttribution(BaseModel):
    model_config = ConfigDict(extra="ignore")

    quote_id: int
    speaker_name: str = Field(min_length=1, max_length=120)


class QuoteClaimAttributionEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore")

    claims: list[QuoteClaimAttribution] = Field(default_factory=list, max_length=12)


def _normalized_words(value: object) -> str:
    text = " ".join(str(value or "").casefold().replace("ё", "е").split())
    return " ".join(_WORD_RE.findall(text))


def _same_surface(left: object, right: object) -> bool:
    left_key = _normalized_words(left)
    right_key = _normalized_words(right)
    if not left_key or not right_key:
        return False
    if left_key == right_key:
        return True
    shorter, longer = sorted((left_key, right_key), key=len)
    return len(shorter) >= 12 and shorter in longer


def _quoted_spans(text: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for match in _QUOTE_RE.finditer(text or ""):
        quote = next((group for group in match.groups() if group is not None), "").strip()
        key = _normalized_words(quote)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(quote)
    return result


def _proposal_evidence(proposal: ProposedChangeCreate) -> str:
    payload = proposal.payload if isinstance(proposal.payload, dict) else {}
    canon = payload.get("_canon") if isinstance(payload.get("_canon"), dict) else {}
    return str(canon.get("evidence") or "")


def _proposal_outcome_id(proposal: ProposedChangeCreate) -> str:
    payload = proposal.payload if isinstance(proposal.payload, dict) else {}
    canon = payload.get("_canon") if isinstance(payload.get("_canon"), dict) else {}
    return str(canon.get("outcome_id") or "")


def _stabilize_narrator_proposals(
    proposals: list[ProposedChangeCreate],
    quoted_spans: list[str],
) -> list[ProposedChangeCreate]:
    """Fail closed when narrator-memory attribution conflicts with immutable quote provenance.

    Quoted speech is epistemic evidence and can never be the sole evidence for objective canon.
    The broad narrator auditor's actor-segment selections are discarded entirely: it has previously
    attributed narrator prose and even player speech to an NPC. A separate narrow quote-only pass
    below recreates only claims whose speaker is explicitly re-adjudicated against present NPCs.
    """
    result: list[ProposedChangeCreate] = []
    objective_types = {
        ChangeType.FACT,
        ChangeType.EVENT,
        ChangeType.RELATIONSHIP,
        ChangeType.MOVEMENT,
        ChangeType.ITEM_TRANSFER,
    }
    for proposal in proposals:
        evidence = _proposal_evidence(proposal)
        evidence_is_quote = bool(
            evidence
            and any(_same_surface(evidence, quote) for quote in quoted_spans)
        )
        if proposal.change_type in objective_types and evidence_is_quote:
            continue
        if (
            proposal.change_type == ChangeType.KNOWLEDGE
            and _proposal_outcome_id(proposal).startswith("actor-segment-")
        ):
            continue
        result.append(proposal)
    return result


def _dedupe(proposals: list[ProposedChangeCreate]) -> list[ProposedChangeCreate]:
    result: list[ProposedChangeCreate] = []
    seen: set[str] = set()
    for proposal in proposals:
        signature = json.dumps(
            {
                "change_type": proposal.change_type.value,
                "payload": proposal.payload,
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        if signature in seen:
            continue
        seen.add(signature)
        result.append(proposal)
    return result


def _has_completed_interaction_receipt(receipts: object) -> bool:
    if not isinstance(receipts, (list, tuple)):
        return False
    for receipt in receipts:
        if not isinstance(receipt, dict):
            continue
        payload = receipt.get("payload") if isinstance(receipt.get("payload"), dict) else {}
        if (
            payload.get("status") == "completed"
            and payload.get("action_type") == "interaction"
            and str(receipt.get("description") or "").strip()
        ):
            return True
    return False


async def _quoted_claim_proposals(
    scribe,
    *,
    campaign_id: UUID,
    scene_id: UUID | None,
    assistant_content: str,
    player_character_id: UUID | None,
    quoted_spans: list[str],
) -> list[ProposedChangeCreate]:
    """Attribute only exact direct-speech spans; never let the model nominate narrator prose."""
    if player_character_id is None or not quoted_spans:
        return []

    from app.services.narrator_memory_audit_guard import _memory_state

    (
        _known_entities,
        _known_ids,
        _participant_ids,
        speaker_names,
        _display_by_id,
        present_npc_names,
    ) = await _memory_state(
        scribe,
        campaign_id,
        scene_id,
        player_character_id,
    )
    if not present_npc_names:
        return []

    selection = await scribe._model_router.resolve(campaign_id, ModelRole.SCRIBE)  # noqa: SLF001
    if selection is None:
        return []

    quote_block = "\n".join(
        f"Q{index}: {quote}" for index, quote in enumerate(quoted_spans, start=1)
    )
    data = await scribe._model_router.generate_json(  # noqa: SLF001
        scribe._llm_provider,  # noqa: SLF001
        selection,
        [
            ChatMessage(
                role="system",
                content=(
                    "[QUOTED NPC CLAIM ATTRIBUTION]\n"
                    "You receive exact immutable quoted spans from an already-published RPG narrator "
                    "response. Select only quotes in which one of PRESENT NPCS actually states a "
                    "concrete factual claim to the player. A claim may be uncertain, mistaken or "
                    "false; do not judge truth. Ignore player speech, greetings, questions, commands, "
                    "pure emotion and non-factual banter. speaker_name must exactly match one PRESENT "
                    "NPC. Return only quote_id + speaker_name. Never select narrator prose because it "
                    "is not present in QUOTES."
                ),
            ),
            ChatMessage(
                role="user",
                content=(
                    "PRESENT NPCS:\n"
                    + ", ".join(present_npc_names)
                    + "\n\nFULL PUBLISHED RESPONSE:\n"
                    + assistant_content
                    + "\n\nQUOTES:\n"
                    + quote_block
                ),
            ),
        ],
        max_tokens=350,
        temperature=0.0,
        response_model=QuoteClaimAttributionEnvelope,
    )
    envelope = QuoteClaimAttributionEnvelope.model_validate(data)

    result: list[ProposedChangeCreate] = []
    used: set[tuple[int, str]] = set()
    for claim in envelope.claims:
        if not 1 <= claim.quote_id <= len(quoted_spans):
            continue
        speaker_id = speaker_names.get(_normalized_words(claim.speaker_name))
        if not speaker_id:
            continue
        marker = (claim.quote_id, speaker_id)
        if marker in used:
            continue
        used.add(marker)
        quote = quoted_spans[claim.quote_id - 1]
        result.append(
            ProposedChangeCreate(
                change_type=ChangeType.KNOWLEDGE,
                payload={
                    "recipient_id": str(player_character_id),
                    "proposition": quote,
                    "source_character_id": str(speaker_id),
                    "confidence": 0.8,
                    "status": "known",
                    "_canon": {
                        "outcome_id": f"quoted-claim-{claim.quote_id}",
                        "kind": "knowledge_transfer",
                        "description": "Игрок услышал это дословное утверждение присутствующего NPC.",
                        "evidence": quote,
                        "authority": "character_claim",
                        "durable": True,
                        "quote_id": claim.quote_id,
                    },
                },
            )
        )
    return result


async def _recover_interaction_facts(
    scribe,
    *,
    campaign_id: UUID,
    scene_id: UUID | None,
    assistant_content: str,
    player_character_id: UUID | None,
    existing_proposals: list[ProposedChangeCreate],
) -> list[ProposedChangeCreate]:
    """One narrow fact-recall pass for a completed non-dialogue interaction receipt."""
    if player_character_id is None:
        return []

    receipts = list(getattr(scribe, "structured_receipts", ()) or ())
    if not _has_completed_interaction_receipt(receipts):
        return []

    from app.services.narrator_memory_audit_guard import _memory_state

    (
        known_entities,
        known_ids,
        participant_ids,
        _speaker_names,
        _display_by_id,
        _present_npc_names,
    ) = await _memory_state(
        scribe,
        campaign_id,
        scene_id,
        player_character_id,
    )
    selection = await scribe._model_router.resolve(campaign_id, ModelRole.SCRIBE)  # noqa: SLF001
    if selection is None:
        return []

    evidence_text = memory_evidence(assistant_content, receipts)
    existing = [
        {
            "change_type": proposal.change_type.value,
            "payload": {
                key: value
                for key, value in proposal.payload.items()
                if key not in {"_canon", "_memory"}
            },
        }
        for proposal in existing_proposals
        if proposal.change_type != ChangeType.CANON_GAP
    ]
    data = await scribe._model_router.generate_json(  # noqa: SLF001
        scribe._llm_provider,  # noqa: SLF001
        selection,
        [
            ChatMessage(
                role="system",
                content=(
                    "[EXECUTED INTERACTION FACT RECOVERY]\n"
                    "Recover only a missing durable OBJECTIVE world-state fact from one completed "
                    "RPG turn. EXECUTED WORLD RESULTS are machine-confirmed outcomes. The published "
                    "narrator text may make the resulting stable state explicit. Return CanonEnvelope "
                    "with zero or more FACT proposals only; do not return knowledge, relationships, "
                    "events, movement, item transfer, theses or narrative details. Never turn quoted "
                    "speech, opinions, claims, questions, player intent, mood or decorative prose into "
                    "objective truth. Do not duplicate EXISTING PROPOSALS. If the executor result and "
                    "narration do not explicitly establish a durable resulting property/state, return "
                    "empty outcomes and proposals. Evidence must be an exact short span from the "
                    "supplied authoritative text. For a changed state, use the actual state-bearing "
                    "subject, a stable predicate, the resulting value, operation=assert/revise, and "
                    "cardinality=single. Human-readable fields must be Russian."
                ),
            ),
            ChatMessage(
                role="user",
                content=(
                    "AUTHORITATIVE TURN RESULT:\n"
                    + evidence_text
                    + "\n\nEXISTING PROPOSALS:\n"
                    + json.dumps(existing, ensure_ascii=False)
                ),
            ),
        ],
        max_tokens=700,
        temperature=0.0,
        response_model=CanonEnvelope,
    )
    envelope = CanonEnvelope.model_validate(data)
    scribe._current_scene_id = scene_id  # noqa: SLF001
    recovered = scribe._parse_data(  # noqa: SLF001
        envelope.model_dump(mode="json"),
        authoritative_text=evidence_text,
        known_entities=known_entities,
        known_ids=known_ids,
        acting_character_id=None,
        player_character_id=player_character_id,
        scene_participant_ids=participant_ids,
    )
    return [proposal for proposal in recovered if proposal.change_type == ChangeType.FACT]


async def _stabilized_narrator_memory(
    original,
    scribe,
    *,
    campaign_id: UUID,
    scene_id: UUID | None,
    assistant_content: str,
    player_character_id: UUID | None,
    base_proposals: list[ProposedChangeCreate],
) -> list[ProposedChangeCreate]:
    result = await original(
        scribe,
        campaign_id=campaign_id,
        scene_id=scene_id,
        assistant_content=assistant_content,
        player_character_id=player_character_id,
        base_proposals=base_proposals,
    )
    quotes = _quoted_spans(assistant_content)
    result = _stabilize_narrator_proposals(result, quotes)

    # Replace the broad actor-segment guesser with one candidate universe: literal quotes only.
    claims: list[ProposedChangeCreate] = []
    if quotes:
        try:
            claims = await _quoted_claim_proposals(
                scribe,
                campaign_id=campaign_id,
                scene_id=scene_id,
                assistant_content=assistant_content,
                player_character_id=player_character_id,
                quoted_spans=quotes,
            )
        except (LLMProviderError, ValueError, TypeError):
            claims = []
        result = _dedupe([*result, *claims])

    # Do not repeat the broad auditor at temperature=0. A missed stable state is repaired by a
    # different, much narrower task with no dialogue-attribution responsibility. Skip quoted turns
    # here so ordinary conversations do not pay for an unnecessary objective-state recovery call.
    if (
        not quotes
        and not any(item.change_type == ChangeType.FACT for item in result)
        and _has_completed_interaction_receipt(getattr(scribe, "structured_receipts", ()))
    ):
        try:
            recovered = await _recover_interaction_facts(
                scribe,
                campaign_id=campaign_id,
                scene_id=scene_id,
                assistant_content=assistant_content,
                player_character_id=player_character_id,
                existing_proposals=result,
            )
        except (LLMProviderError, ValueError, TypeError):
            recovered = []
        recovered = _stabilize_narrator_proposals(recovered, quotes)
        result = _dedupe([*result, *recovered])

    return result


async def _deterministic_debt_fallback(processor, job_id: UUID) -> None:
    """Close only an exact item-backed debt that survived the normal receipt reconciler.

    This is a legacy-projection bridge only. It does not run in TE2 writer mode. A `revise` creates
    an extracted replacement row rather than simply mutating the manual assertion, so ActiveCanonReplay
    can restore the old relation on /undo.
    """
    if settings.TE2_SEMANTIC_MODE == "writer":
        return

    from app.db.repositories.proposed_change_repo import ProposedChangeRepository
    from app.db.tables import PostTurnJob
    from app.services.post_turn_structured_receipt_guard import (
        _executed_steps,
        _explicit_item_debt_fulfillments,
        _player_id,
        _relationship_candidates,
    )

    row = await processor._session.get(PostTurnJob, str(job_id))  # noqa: SLF001
    if row is None or row.status != "completed" or row.job_type != "memory_scribe":
        return
    assistant = await processor._turns.get_by_id(UUID(row.assistant_turn_id))  # noqa: SLF001
    if (
        assistant is None
        or assistant.status != "active"
        or not assistant.parent_turn_id
        or not processor._authority_managed(assistant)  # noqa: SLF001
    ):
        return
    player_id = _player_id(assistant)
    if player_id is None:
        return

    proposal_repo = ProposedChangeRepository(processor._session)  # noqa: SLF001
    existing = await proposal_repo.get_for_turn(assistant.id)
    external_resolution = await processor._uses_external_proposal_resolution(  # noqa: SLF001
        assistant.parent_turn_id
    )

    for step in _executed_steps(assistant):
        if not (
            step.get("status") == "completed"
            and step.get("action_type") == "inventory"
            and step.get("item_operation") == "give"
            and step.get("item_result_owner_id")
        ):
            continue
        try:
            target_id = UUID(str(step["item_result_owner_id"]))
        except (TypeError, ValueError):
            continue
        relationships = await _relationship_candidates(
            processor,
            UUID(row.campaign_id),
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
        matched = _explicit_item_debt_fulfillments(receipt, relationships)
        by_id = {UUID(str(item.id)): item for item in relationships}
        for relationship_id in matched:
            current = by_id.get(relationship_id)
            if current is None or not current.is_current:
                continue
            if any(
                proposal.change_type == ChangeType.RELATIONSHIP.value
                and str((proposal.payload or {}).get("_receipt_debt_fallback_id"))
                == str(relationship_id)
                for proposal in existing
            ):
                continue
            item_name = str(step.get("item_name") or "предмет").split(" (", 1)[0]
            payload: dict[str, Any] = {
                "subject_id": str(current.subject_id),
                "object_id": str(current.object_id),
                "relation_type": current.relation_type,
                "description": f"Обязательство выполнено: {item_name} возвращён; долг закрыт.",
                "reason": "Structured give receipt exactly fulfilled the item-backed debt condition.",
                "intensity": 0.0,
                "visibility": current.visibility,
                "operation": "revise",
                "cardinality": "single",
                "_structured_receipt": receipt,
                "_receipt_debt_fallback_id": str(relationship_id),
            }
            created = await proposal_repo.create_batch(
                assistant.id,
                [ProposedChangeCreate(change_type=ChangeType.RELATIONSHIP, payload=payload)],
            )
            proposal = created[0]
            existing.append(proposal)
            if external_resolution:
                continue
            await CanonApplier(processor._session).apply(  # noqa: SLF001
                UUID(row.campaign_id),
                ChangeType.RELATIONSHIP,
                payload,
                assistant.id,
            )
            await proposal_repo.resolve(
                proposal.id,
                ProposalAction(status="accepted"),
            )
    await processor._session.commit()  # noqa: SLF001


def install() -> None:
    """Install bounded correctness guards after the broad semantic/runtime guards."""
    global _INSTALLED
    if _INSTALLED:
        return

    import app.services.narrator_memory_audit_guard as narrator_memory_module
    from app.services.post_turn_processor import PostTurnProcessor
    from app.services.turn_authority_planner import TurnAuthorityPlanner

    original_enrich = narrator_memory_module.enrich_narrator_memory

    async def stabilized_enrich(
        scribe,
        *,
        campaign_id,
        scene_id,
        assistant_content,
        player_character_id,
        base_proposals,
    ):
        return await _stabilized_narrator_memory(
            original_enrich,
            scribe,
            campaign_id=campaign_id,
            scene_id=scene_id,
            assistant_content=assistant_content,
            player_character_id=player_character_id,
            base_proposals=base_proposals,
        )

    narrator_memory_module.enrich_narrator_memory = stabilized_enrich

    # Bound the composed Planner control plane. Individual loops were already finite, but nested
    # reviewer/adjudicator/profile/recovery loops could multiply into 30+ model requests and drift a
    # good initial plan into a worse one. Twelve top-level planner calls is deliberately above the
    # observed healthy path while making repair cascades impossible.
    original_router_generate_json = RoleModelRouter.generate_json

    async def budgeted_generate_json(self, provider, selection, messages, **kwargs):
        state = _PLANNER_CALL_STATE.get()
        role = getattr(getattr(selection, "role", None), "value", None)
        if state is not None and role == ModelRole.PLANNER.value:
            if state["used"] >= state["limit"]:
                raise LLMProviderError(
                    f"planner control-call budget exceeded ({state['used']}/{state['limit']})"
                )
            state["used"] += 1
        return await original_router_generate_json(
            self,
            provider,
            selection,
            messages,
            **kwargs,
        )

    RoleModelRouter.generate_json = budgeted_generate_json

    original_plan = TurnAuthorityPlanner.plan

    async def budgeted_plan(self, *args, **kwargs):
        token = _PLANNER_CALL_STATE.set({"used": 0, "limit": _PLANNER_CALL_BUDGET})
        try:
            return await original_plan(self, *args, **kwargs)
        finally:
            _PLANNER_CALL_STATE.reset(token)

    TurnAuthorityPlanner.plan = budgeted_plan

    # This wrapper is installed after the existing structured-receipt reconciler. It is therefore a
    # true fallback: normally there is nothing left to do; if an exact debt somehow survived, close
    # it deterministically with an undo-safe extracted revision.
    original_process_job = PostTurnProcessor.process_job

    async def quality_aware_process_job(self, job_id, *, already_claimed=False):
        await original_process_job(self, job_id, already_claimed=already_claimed)
        try:
            await _deterministic_debt_fallback(self, job_id)
        except Exception:
            await self._session.rollback()  # noqa: SLF001
            raise

    PostTurnProcessor.process_job = quality_aware_process_job
    _INSTALLED = True


__all__ = [
    "QuoteClaimAttribution",
    "QuoteClaimAttributionEnvelope",
    "_PLANNER_CALL_BUDGET",
    "_has_completed_interaction_receipt",
    "_quoted_spans",
    "_recover_interaction_facts",
    "_stabilize_narrator_proposals",
    "install",
]
