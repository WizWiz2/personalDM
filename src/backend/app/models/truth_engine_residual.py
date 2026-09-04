from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class ResidualEntityMention(BaseModel):
    ref: str = Field(min_length=1, max_length=48, pattern=r"^[A-Za-z0-9_-]+$")
    mention_text: str = Field(min_length=1, max_length=500)
    entity_type: str = Field(min_length=1, max_length=50)
    description: str | None = Field(default=None, max_length=1600)
    mention_kind: str | None = Field(default=None, max_length=64)
    evidence: str | None = Field(default=None, max_length=1600)


class ResidualFluentObservation(BaseModel):
    # The model may suggest a local key, but backend sanitation replaces it with a deterministic
    # content fingerprint before this observation is allowed into the canonical residual graph.
    atom_key: str | None = Field(
        default=None,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    subject_ref: str = Field(min_length=1, max_length=48)
    semantic_description: str = Field(min_length=1, max_length=1200)
    value: Any
    description: str = Field(min_length=1, max_length=1600)
    evidence: str | None = Field(default=None, max_length=1600)
    cardinality_hint: Literal["single", "multi"] | None = None


class ResidualRelationObservation(BaseModel):
    # See ResidualFluentObservation.atom_key. Local idempotency is backend-owned, not an LLM task.
    atom_key: str | None = Field(
        default=None,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    subject_ref: str = Field(min_length=1, max_length=48)
    object_ref: str = Field(min_length=1, max_length=48)
    semantic_description: str = Field(min_length=1, max_length=1200)
    present: bool = True
    description: str = Field(min_length=1, max_length=1600)
    evidence: str | None = Field(default=None, max_length=1600)
    cardinality_hint: Literal["single", "multi"] | None = None


class ResidualSanitizationAudit(BaseModel):
    duplicate_entity_refs_dropped: int = 0
    dangling_fluents_dropped: int = 0
    dangling_relations_dropped: int = 0
    duplicate_fluents_dropped: int = 0
    duplicate_relations_dropped: int = 0


class RawSemanticResidualEnvelope(BaseModel):
    """Shape-valid model output before backend local-graph sanitation."""

    entities: list[ResidualEntityMention] = Field(default_factory=list, max_length=16)
    fluents: list[ResidualFluentObservation] = Field(default_factory=list, max_length=16)
    relations: list[ResidualRelationObservation] = Field(default_factory=list, max_length=16)


class SanitizedResidual(BaseModel):
    envelope: "SemanticResidualEnvelope"
    audit: ResidualSanitizationAudit


def _content_key(prefix: str, atom: dict[str, Any]) -> str:
    payload = {key: value for key, value in atom.items() if key != "atom_key"}
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"


def _sanitize_payload(data: Any) -> tuple[Any, ResidualSanitizationAudit]:
    """Own local graph bookkeeping without making any semantic judgement."""

    if isinstance(data, BaseModel):
        data = data.model_dump(mode="json")
    if not isinstance(data, dict):
        return data, ResidualSanitizationAudit()

    raw_entities = data.get("entities") or []
    entities: list[Any] = []
    known: set[str] = set()
    duplicate_entity_refs_dropped = 0
    for entity in raw_entities:
        if not isinstance(entity, dict):
            entities.append(entity)
            continue
        ref = entity.get("ref")
        if isinstance(ref, str) and ref in known:
            duplicate_entity_refs_dropped += 1
            continue
        if isinstance(ref, str):
            known.add(ref)
        entities.append(entity)

    dangling_fluents_dropped = 0
    duplicate_fluents_dropped = 0
    fluents: list[Any] = []
    fluent_keys: set[str] = set()
    for atom in data.get("fluents") or []:
        if not isinstance(atom, dict):
            fluents.append(atom)
            continue
        if atom.get("subject_ref") not in known:
            dangling_fluents_dropped += 1
            continue
        atom_key = _content_key("f", atom)
        if atom_key in fluent_keys:
            duplicate_fluents_dropped += 1
            continue
        fluent_keys.add(atom_key)
        fluents.append({**atom, "atom_key": atom_key})

    dangling_relations_dropped = 0
    duplicate_relations_dropped = 0
    relations: list[Any] = []
    relation_keys: set[str] = set()
    for atom in data.get("relations") or []:
        if not isinstance(atom, dict):
            relations.append(atom)
            continue
        if atom.get("subject_ref") not in known or atom.get("object_ref") not in known:
            dangling_relations_dropped += 1
            continue
        atom_key = _content_key("r", atom)
        if atom_key in relation_keys:
            duplicate_relations_dropped += 1
            continue
        relation_keys.add(atom_key)
        relations.append({**atom, "atom_key": atom_key})

    return (
        {
            **data,
            "entities": entities,
            "fluents": fluents,
            "relations": relations,
        },
        ResidualSanitizationAudit(
            duplicate_entity_refs_dropped=duplicate_entity_refs_dropped,
            dangling_fluents_dropped=dangling_fluents_dropped,
            dangling_relations_dropped=dangling_relations_dropped,
            duplicate_fluents_dropped=duplicate_fluents_dropped,
            duplicate_relations_dropped=duplicate_relations_dropped,
        ),
    )


class SemanticResidualEnvelope(RawSemanticResidualEnvelope):
    """Sanitized objective semantic residue ready for identity/schema resolution.

    Local refs exist only inside this envelope. They are not persistent IDs and cannot be used as
    database identity. Atom keys are backend-generated exact-content fingerprints; semantic identity
    is still resolved separately through stable Entity/SemanticType UUIDs.
    """

    @model_validator(mode="before")
    @classmethod
    def sanitize_local_graph(cls, data):
        sanitized, _ = _sanitize_payload(data)
        return sanitized

    @model_validator(mode="after")
    def validate_local_graph(self):
        refs = [entity.ref for entity in self.entities]
        if len(refs) != len(set(refs)):
            raise ValueError("semantic residual entity refs must be unique")
        known = set(refs)
        atom_keys = [atom.atom_key for atom in self.fluents] + [
            atom.atom_key for atom in self.relations
        ]
        if any(not atom_key for atom_key in atom_keys):
            raise ValueError("sanitized semantic residual atoms require backend atom keys")
        if len(atom_keys) != len(set(atom_keys)):
            raise ValueError("semantic residual atom keys must be unique")
        for fluent in self.fluents:
            if fluent.subject_ref not in known:
                raise ValueError(
                    f"fluent {fluent.atom_key} references unknown subject_ref {fluent.subject_ref}"
                )
        for relation in self.relations:
            if relation.subject_ref not in known:
                raise ValueError(
                    f"relation {relation.atom_key} references unknown subject_ref {relation.subject_ref}"
                )
            if relation.object_ref not in known:
                raise ValueError(
                    f"relation {relation.atom_key} references unknown object_ref {relation.object_ref}"
                )
        return self


def sanitize_semantic_residual(raw: RawSemanticResidualEnvelope) -> SanitizedResidual:
    """Return a sanitized envelope plus bookkeeping diagnostics.

    This function never decides semantic truth. It only performs deterministic local-graph hygiene so
    one malformed atom cannot erase unrelated valid observations from the turn.
    """

    payload, audit = _sanitize_payload(raw)
    return SanitizedResidual(
        envelope=SemanticResidualEnvelope.model_validate(payload),
        audit=audit,
    )


SanitizedResidual.model_rebuild()
