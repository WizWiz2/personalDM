from __future__ import annotations

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
    atom_key: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    subject_ref: str = Field(min_length=1, max_length=48)
    semantic_description: str = Field(min_length=1, max_length=1200)
    value: Any
    description: str = Field(min_length=1, max_length=1600)
    evidence: str | None = Field(default=None, max_length=1600)
    cardinality_hint: Literal["single", "multi"] | None = None


class ResidualRelationObservation(BaseModel):
    atom_key: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    subject_ref: str = Field(min_length=1, max_length=48)
    object_ref: str = Field(min_length=1, max_length=48)
    semantic_description: str = Field(min_length=1, max_length=1200)
    present: bool = True
    description: str = Field(min_length=1, max_length=1600)
    evidence: str | None = Field(default=None, max_length=1600)
    cardinality_hint: Literal["single", "multi"] | None = None


class SemanticResidualEnvelope(BaseModel):
    """Objective semantic residue after deterministic executor receipts are accounted for.

    Local refs exist only inside this envelope. They are not persistent IDs and cannot be used as
    database identity. The compiler resolves them to stable Entity UUIDs before any world mutation.
    """

    entities: list[ResidualEntityMention] = Field(default_factory=list, max_length=16)
    fluents: list[ResidualFluentObservation] = Field(default_factory=list, max_length=16)
    relations: list[ResidualRelationObservation] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def validate_local_graph(self):
        refs = [entity.ref for entity in self.entities]
        if len(refs) != len(set(refs)):
            raise ValueError("semantic residual entity refs must be unique")
        known = set(refs)
        atom_keys = [atom.atom_key for atom in self.fluents] + [
            atom.atom_key for atom in self.relations
        ]
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
