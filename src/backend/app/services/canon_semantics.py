from __future__ import annotations

import json
import re
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

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
    ]
    description: str = Field(min_length=3, max_length=600)
    evidence: str = Field(min_length=1, max_length=600)
    authority: CanonAuthority
    durable: bool = True


class ProposalAtom(BaseModel):
    outcome_id: str
    change_type: ChangeType
    operation: CanonOperation = CanonOperation.ASSERT
    cardinality: FactCardinality = FactCardinality.SINGLE
    payload: dict[str, Any] = Field(default_factory=dict)


class CanonEnvelope(BaseModel):
    outcomes: list[OutcomeAtom] = Field(default_factory=list, max_length=10)
    proposals: list[ProposalAtom] = Field(default_factory=list, max_length=12)


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
    proposal_count: int = 0
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


def proposals_from_envelope(
    data: dict[str, Any],
    authoritative_text: str,
) -> tuple[list[ProposedChangeCreate], CanonAudit]:
    """Validate outcome evidence, authority and proposal coverage deterministically."""
    if not isinstance(data, dict):
        return [], CanonAudit(envelope_valid=False, error="Scribe response is not an object")

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

    audit = CanonAudit(
        outcome_count=len(envelope.outcomes),
        durable_outcome_count=sum(1 for item in envelope.outcomes if item.durable),
    )
    outcomes: dict[str, OutcomeAtom] = {}
    supported: set[str] = set()
    for outcome in envelope.outcomes[:10]:
        outcome_id = normalize_key(outcome.id)
        if not outcome_id or outcome_id in outcomes:
            audit.rejected_schema_count += 1
            continue
        outcomes[outcome_id] = outcome
        if evidence_supported(outcome.evidence, authoritative_text):
            supported.add(outcome_id)
        else:
            audit.rejected_evidence_count += 1

    audit.supported_outcome_count = len(supported)
    covered: set[str] = set()
    results: list[ProposedChangeCreate] = []
    seen: set[str] = set()

    for proposal in envelope.proposals[:12]:
        outcome_id = normalize_key(proposal.outcome_id)
        outcome = outcomes.get(outcome_id)
        if not outcome or outcome_id not in supported:
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
        if proposal.change_type == ChangeType.FACT:
            payload.setdefault("operation", proposal.operation.value)
            payload.setdefault("cardinality", proposal.cardinality.value)

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
        if outcome.durable and outcome_id in supported
    }
    gaps = sorted(durable_supported - covered)
    for outcome_id in gaps:
        outcome = outcomes[outcome_id]
        results.append(
            ProposedChangeCreate(
                change_type=ChangeType.CANON_GAP,
                payload={
                    "_validation_error": "Durable confirmed outcome has no structured canon delta",
                    "_canon": {
                        "outcome_id": outcome_id,
                        "kind": outcome.kind,
                        "description": outcome.description,
                        "evidence": outcome.evidence,
                        "authority": outcome.authority.value,
                    },
                },
            )
        )

    audit.covered_outcome_count = len(durable_supported & covered)
    audit.gap_count = len(gaps)
    audit.gap_outcome_ids = gaps
    audit.proposal_count = len(results)
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
