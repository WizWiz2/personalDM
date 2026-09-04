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


class RawSemanticResidualEnvelope(BaseModel):
    """Shape-valid model output before backend local-graph sanitation.

    A small control model is allowed to make local bookkeeping mistakes such as reusing an atom key
    or referencing a local entity token it forgot to declare. Those mistakes must not erase unrelated
    valid observations from the same turn. Semantic truth is still bounded later; this layer only
    makes local graph bookkeeping deterministic.
    """

    entities: list[ResidualEntityMention] = Field(default_factory=list, max_length=16)
    fluents: list[ResidualFluentObservation] = Field(default_factory=list, max_length=16)
    relations: list[ResidualRelationObservation] = Field(default_factory=list, max_length=16)


class SemanticResidualEnvelope(RawSemanticResidualEnvelope):
    """Sanitized objective semantic residue ready for identity/schema resolution.

    Local refs exist only inside this envelope. They are not persistent IDs and cannot be used as
    database identity. Atom keys are backend-generated exact-content fingerprints; semantic identity
    is still resolved separately through stable Entity/SemanticType UUIDs.
    """

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


class ResidualSanitizationAudit(BaseModel):
    duplicate_entity_refs_dropped: int = 0
    dangling_fluents_dropped: int = 0
    dangling_relations_dropped: int = 0
    duplicate_fluents_dropped: int = 0
    duplicate_relations_dropped: int = 0


class SanitizedResidual(BaseModel):
    envelope: SemanticResidualEnvelope
    audit: ResidualSanitizationAudit


def _content_key(prefix: str, atom: BaseModel) -> str:
    payload = atom.model_dump(mode="json", exclude={"atom_key"})
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"


def sanitize_semantic_residual(raw: RawSemanticResidualEnvelope) -> SanitizedResidual:
    """Turn shape-valid model output into a deterministic local observation graph.

    This function does not decide world semantics. It only owns bookkeeping that should never have
    been delegated to an LLM: unique local entity tokens, valid local references and atom idempotency
    keys. A malformed atom is dropped independently instead of invalidating unrelated observations.
    """

    duplicate_entity_refs_dropped = 0
    entities: list[ResidualEntityMention] = []
    known: set[str] = set()
    for entity in raw.entities:
        if entity.ref in known:
            duplicate_entity_refs_dropped += 1
            continue
        known.add(entity.ref)
        entities.append(entity)

    dangling_fluents_dropped = 0
    duplicate_fluents_dropped = 0
    fluents: list[ResidualFluentObservation] = []
    fluent_keys: set[str] = set()
    for atom in raw.fluents:
        if atom.subject_ref not in known:
            dangling_fluents_dropped += 1
            continue
        atom_key = _content_key("f", atom)
        if atom_key in fluent_keys:
            duplicate_fluents_dropped += 1
            continue
        fluent_keys.add(atom_key)
        fluents.append(atom.model_copy(update={"atom_key": atom_key}))

    dangling_relations_dropped = 0
    duplicate_relations_dropped = 0
    relations: list[ResidualRelationObservation] = []
    relation_keys: set[str] = set()
    for atom in raw.relations:
        if atom.subject_ref not in known or atom.object_ref not in known:
            dangling_relations_dropped += 1
            continue
        atom_key = _content_key("r", atom)
        if atom_key in relation_keys:
            duplicate_relations_dropped += 1
            continue
        relation_keys.add(atom_key)
        relations.append(atom.model_copy(update={"atom_key": atom_key}))

    return SanitizedResidual(
        envelope=SemanticResidualEnvelope(
            entities=entities,
            fluents=fluents,
            relations=relations,
        ),
        audit=ResidualSanitizationAudit(
            duplicate_entity_refs_dropped=duplicate_entity_refs_dropped,
            dangling_fluents_dropped=dangling_fluents_dropped,
            dangling_relations_dropped=dangling_relations_dropped,
            duplicate_fluents_dropped=duplicate_fluents_dropped,
            duplicate_relations_dropped=duplicate_relations_dropped,
        ),
    )
