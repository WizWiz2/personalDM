from __future__ import annotations

import json
import re
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from app.models.memory_semantics import MemoryClass, MemoryMetadata, MemoryRetention
from app.models.proposed_change import ChangeType, ProposedChangeCreate


WORD_PATTERN = re.compile(r"[\w-]+", flags=re.UNICODE)


class CanonAuthority(str, Enum):
    DM_CONFIRMED = "dm_confirmed"
    PUBLIC_OBSERVATION = "public_observation"
    CHARACTER_CLAIM = "character_claim"
    PLAYER_INTENT = "player_intent"


class CanonOperation(str, Enum):
    ASSERT = "assert"
    REVISE = "revise"
    RETRACT = "retract"
    CONTRADICT = "contradict"


class FactCardinality(str, Enum):
    SINGLE = "single"
    MULTI = "multi"


class OutcomeAtom(BaseModel):
    id: str = Field(min_length=1, max_length=40)
    kind: Literal[
        "world_state",
        "event",
        "knowledge_transfer",
        "relationship_change",
        "movement",
        "item_transfer",
        "narrative_detail",
    ]
    description: str = Field(min_length=3, max_length=600)
    evidence: str = Field(min_length=1, max_length=600)
    authority: CanonAuthority
    durable: bool = True
    memory_class: MemoryClass = MemoryClass.WORLD_CANON
    retention: MemoryRetention = MemoryRetention.DURABLE
    ttl_turns: int | None = Field(default=None, ge=1, le=8)


class ProposalAtom(BaseModel):
    outcome_id: str
    change_type: ChangeType
    operation: CanonOperation = CanonOperation.ASSERT
    cardinality: FactCardinality = FactCardinality.SINGLE
    payload: dict[str, Any] = Field(default_factory=dict)


class CanonEnvelope(BaseModel):
    outcomes: list[OutcomeAtom] = Field(default_factory=list, max_length=12)
    proposals: list[ProposalAtom] = Field(default_factory=list, max_length=14)


class CanonAudit(BaseModel):
    envelope_valid: bool = True
    legacy_envelope: bool = False
    outcome_count: int = 0
    durable_outcome_count: int = 0
    supported_outcome_count: int = 0
    covered_outcome_count: int = 0
    gap_count: int = 0
    gap_outcome_ids: list[str] = Field(default_factory=list)
    rejected_evidence_count: int = 0
    rejected_authority_count: int = 0
    rejected_schema_count: int = 0
    duplicate_proposal_count: int = 0
    inferred_memory_count: int = 0
    proposal_count: int = 0
    detail_count: int = 0
    coverage_ratio: float = 1.0
    error: str | None = None


def normalize_text(value: object) -> str:
    if value is None:
        return ""
    return " ".join(str(value).casefold().split())


def normalize_key(value: object) -> str:
    return " ".join(WORD_PATTERN.findall(normalize_text(value)))


def evidence_supported(evidence: str, authoritative_text: str) -> bool:
    """Require an extractive or strongly overlapping evidence span from the DM result."""
    evidence_norm = normalize_text(evidence)
    authoritative_norm = normalize_text(authoritative_text)
    if not evidence_norm or not authoritative_norm:
        return False
    if evidence_norm in authoritative_norm:
        return True

    evidence_words = set(WORD_PATTERN.findall(evidence_norm))
    authoritative_words = set(WORD_PATTERN.findall(authoritative_norm))
    if len(evidence_words) < 3:
        return False
    overlap = len(evidence_words & authoritative_words) / len(evidence_words)
    return overlap >= 0.75


def authority_allows(authority: CanonAuthority, change_type: ChangeType) -> bool:
    if authority in {CanonAuthority.DM_CONFIRMED, CanonAuthority.PUBLIC_OBSERVATION}:
        return change_type != ChangeType.CANON_GAP
    if authority == CanonAuthority.CHARACTER_CLAIM:
        return change_type == ChangeType.KNOWLEDGE
    return False


def _legacy_proposals(data: dict[str, Any]) -> list[ProposedChangeCreate]:
    results: list[ProposedChangeCreate] = []
    for raw in data.get("proposals", [])[:10]:
        try:
            change_type = ChangeType(raw.get("change_type"))
        except (ValueError, TypeError, AttributeError):
            continue
        payload = raw.get("payload")
        if not isinstance(payload, dict) or not payload:
            continue
        results.append(ProposedChangeCreate(change_type=change_type, payload=payload))
    return results


def _memory_metadata(outcome: OutcomeAtom) -> MemoryMetadata | None:
    try:
        return MemoryMetadata(
            memory_class=outcome.memory_class,
            retention=outcome.retention,
            ttl_turns=outcome.ttl_turns,
        )
    except ValidationError:
        return None


def _memory_is_explicit(outcome: OutcomeAtom) -> bool:
    return bool(
        {"memory_class", "retention", "ttl_turns"} & outcome.model_fields_set
    )


def _infer_memory(
    change_type: ChangeType,
    payload: dict[str, Any],
) -> MemoryMetadata:
    if change_type == ChangeType.NARRATIVE_DETAIL:
        return MemoryMetadata(
            memory_class=MemoryClass.NARRATIVE_DETAIL,
            retention=MemoryRetention.RECENT_TURNS,
            ttl_turns=payload.get("ttl_turns", 3),
        )
    if change_type == ChangeType.FACT:
        if payload.get("scope") == "scene":
            return MemoryMetadata(
                memory_class=MemoryClass.SCENE_STATE,
                retention=MemoryRetention.SCENE_LIFETIME,
            )
        if payload.get("subject_entity_id"):
            return MemoryMetadata(
                memory_class=MemoryClass.ENTITY_STATE,
                retention=MemoryRetention.UNTIL_SUPERSEDED,
            )
        return MemoryMetadata(
            memory_class=MemoryClass.WORLD_CANON,
            retention=MemoryRetention.DURABLE,
        )
    if change_type == ChangeType.EVENT:
        return MemoryMetadata(
            memory_class=MemoryClass.WORLD_CANON,
            retention=MemoryRetention.DURABLE,
        )
    return MemoryMetadata(
        memory_class=MemoryClass.ENTITY_STATE,
        retention=MemoryRetention.UNTIL_SUPERSEDED,
    )


def _memory_matches(change_type: ChangeType, memory_class: MemoryClass) -> bool:
    if change_type == ChangeType.NARRATIVE_DETAIL:
        return memory_class == MemoryClass.NARRATIVE_DETAIL
    if change_type == ChangeType.FACT:
        return memory_class in {
            MemoryClass.WORLD_CANON,
            MemoryClass.ENTITY_STATE,
            MemoryClass.SCENE_STATE,
        }
    if change_type == ChangeType.EVENT:
        return memory_class == MemoryClass.WORLD_CANON
    if change_type in {
        ChangeType.RELATIONSHIP,
        ChangeType.MOVEMENT,
        ChangeType.KNOWLEDGE,
        ChangeType.ITEM_TRANSFER,
    }:
        return memory_class == MemoryClass.ENTITY_STATE
    return True


def proposals_from_envelope(
    data: dict[str, Any],
    authoritative_text: str,
) -> tuple[list[ProposedChangeCreate], CanonAudit]:
    """Validate evidence, authority, memory lifecycle and proposal coverage."""
    if not isinstance(data, dict):
        return [], CanonAudit(
            envelope_valid=False,
            error="Scribe response is not an object",
        )

    if "outcomes" not in data:
        proposals = _legacy_proposals(data)
        return proposals, CanonAudit(
            envelope_valid=not proposals,
            legacy_envelope=True,
            proposal_count=len(proposals),
            error=("Legacy proposal envelope has no outcome evidence" if proposals else None),
        )

    try:
        envelope = CanonEnvelope.model_validate(data)
    except ValidationError as exc:
        return [], CanonAudit(
            envelope_valid=False,
            rejected_schema_count=1,
            error=str(exc),
        )

    audit = CanonAudit(outcome_count=len(envelope.outcomes))
    outcomes: dict[str, OutcomeAtom] = {}
    memories: dict[str, MemoryMetadata] = {}
    explicit_memory: set[str] = set()
    supported: set[str] = set()
    for outcome in envelope.outcomes[:12]:
        outcome_id = normalize_key(outcome.id)
        if not outcome_id or outcome_id in outcomes:
            audit.rejected_schema_count += 1
            continue
        memory = _memory_metadata(outcome)
        if memory is None:
            audit.rejected_schema_count += 1
            continue
        if _memory_is_explicit(outcome):
            explicit_memory.add(outcome_id)
            if memory.memory_class == MemoryClass.NARRATIVE_DETAIL and outcome.durable:
                audit.rejected_schema_count += 1
                continue
            if memory.memory_class != MemoryClass.NARRATIVE_DETAIL and not outcome.durable:
                audit.rejected_schema_count += 1
                continue
        outcomes[outcome_id] = outcome
        memories[outcome_id] = memory
        if evidence_supported(outcome.evidence, authoritative_text):
            supported.add(outcome_id)
        else:
            audit.rejected_evidence_count += 1

    audit.supported_outcome_count = len(supported)
    covered: set[str] = set()
    results: list[ProposedChangeCreate] = []
    seen: set[str] = set()

    for proposal in envelope.proposals[:14]:
        outcome_id = normalize_key(proposal.outcome_id)
        outcome = outcomes.get(outcome_id)
        memory = memories.get(outcome_id)
        if not outcome or not memory or outcome_id not in supported:
            audit.rejected_evidence_count += 1
            continue
        if not authority_allows(outcome.authority, proposal.change_type):
            audit.rejected_authority_count += 1
            continue
        if proposal.change_type in {ChangeType.SCENE_THESIS, ChangeType.CANON_GAP}:
            audit.rejected_schema_count += 1
            continue
        if not proposal.payload:
            audit.rejected_schema_count += 1
            continue

        if outcome_id not in explicit_memory:
            inferred = _infer_memory(proposal.change_type, proposal.payload)
            previous = memories.get(outcome_id)
            if previous != inferred:
                if outcome_id in covered:
                    audit.rejected_schema_count += 1
                    continue
                memories[outcome_id] = inferred
                memory = inferred
                audit.inferred_memory_count += 1
        if not _memory_matches(proposal.change_type, memory.memory_class):
            audit.rejected_schema_count += 1
            continue

        payload = dict(proposal.payload)
        payload["_canon"] = {
            "outcome_id": outcome_id,
            "kind": outcome.kind,
            "description": outcome.description,
            "evidence": outcome.evidence,
            "authority": outcome.authority.value,
            "operation": proposal.operation.value,
            "cardinality": proposal.cardinality.value,
        }
        payload["_memory"] = {
            "class": memory.memory_class.value,
            "retention": memory.retention.value,
            "ttl_turns": memory.ttl_turns,
        }
        if proposal.change_type == ChangeType.FACT:
            payload.setdefault("operation", proposal.operation.value)
            payload.setdefault("cardinality", proposal.cardinality.value)
            payload.setdefault("memory_class", memory.memory_class.value)
        elif proposal.change_type == ChangeType.EVENT:
            payload.setdefault("memory_class", memory.memory_class.value)
        elif proposal.change_type == ChangeType.NARRATIVE_DETAIL:
            payload.setdefault("ttl_turns", memory.ttl_turns or 3)

        signature = json.dumps(
            {"change_type": proposal.change_type.value, "payload": payload},
            ensure_ascii=False,
            sort_keys=True,
        )
        if signature in seen:
            audit.duplicate_proposal_count += 1
            continue
        seen.add(signature)
        covered.add(outcome_id)
        results.append(
            ProposedChangeCreate(change_type=proposal.change_type, payload=payload)
        )

    durable_supported = {
        outcome_id
        for outcome_id, outcome in outcomes.items()
        if outcome.durable
        and memories[outcome_id].memory_class != MemoryClass.NARRATIVE_DETAIL
        and outcome_id in supported
    }
    audit.durable_outcome_count = len(
        {
            outcome_id
            for outcome_id, outcome in outcomes.items()
            if outcome.durable
            and memories[outcome_id].memory_class != MemoryClass.NARRATIVE_DETAIL
        }
    )
    gaps = sorted(durable_supported - covered)
    for outcome_id in gaps:
        outcome = outcomes[outcome_id]
        memory = memories[outcome_id]
        results.append(
            ProposedChangeCreate(
                change_type=ChangeType.CANON_GAP,
                payload={
                    "_validation_error": (
                        "Durable confirmed outcome has no structured canon delta"
                    ),
                    "_canon": {
                        "outcome_id": outcome_id,
                        "kind": outcome.kind,
                        "description": outcome.description,
                        "evidence": outcome.evidence,
                        "authority": outcome.authority.value,
                    },
                    "_memory": {
                        "class": memory.memory_class.value,
                        "retention": memory.retention.value,
                    },
                },
            )
        )

    audit.covered_outcome_count = len(durable_supported & covered)
    audit.gap_count = len(gaps)
    audit.gap_outcome_ids = gaps
    audit.proposal_count = len(results)
    audit.detail_count = sum(
        1 for item in results if item.change_type == ChangeType.NARRATIVE_DETAIL
    )
    audit.coverage_ratio = (
        audit.covered_outcome_count / len(durable_supported)
        if durable_supported
        else 1.0
    )
    audit.envelope_valid = (
        audit.rejected_schema_count == 0
        and audit.rejected_evidence_count == 0
        and audit.rejected_authority_count == 0
        and audit.gap_count == 0
    )
    return results, audit
