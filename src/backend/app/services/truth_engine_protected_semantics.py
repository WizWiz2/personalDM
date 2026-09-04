from __future__ import annotations

import json
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.truth_engine_table import SemanticType
from app.models.truth_engine import (
    SemanticTypeResolutionCandidate,
    SemanticTypeResolutionDecision,
)
from app.models.turn import ChatMessage
from app.services.role_model_router import ModelRole
from app.services.truth_engine_semantics import (
    ConstrainedSemanticResolver,
    SemanticResolutionError,
)


class ProtectedAwareSemanticResolver(ConstrainedSemanticResolver):
    """Resolve open semantics while exposing engine-owned slots only for collision recognition.

    Hiding `core.*` slots entirely lets a model describe the same concept and choose NEW, creating a
    semantic duplicate beside deterministic executor state. This resolver shows those slots with their
    `system_key`, instructs the model to select them when the concept is the same, and leaves the writer
    responsible for treating that selection as a collision/no-op rather than a mutable target.
    """

    def __init__(self, session: AsyncSession, **kwargs):
        super().__init__(session, **kwargs)
        self._protected_session = session

    async def resolve_semantic_type(
        self,
        campaign_id: UUID,
        *,
        kind: Literal["fluent", "relation"],
        semantic_description: str,
        subject_entity_id: UUID,
        observed_value: object | None = None,
        cardinality_hint: Literal["single", "multi"] | None = None,
    ) -> tuple[SemanticTypeResolutionDecision, list[SemanticTypeResolutionCandidate]]:
        candidates = await self._retriever.semantic_type_candidates(
            campaign_id,
            kind=kind,
            subject_entity_id=subject_entity_id,
            include_system_types=True,
        )
        candidates = await self._hydrate_system_keys(candidates)
        selection = await self._model_router.resolve(campaign_id, ModelRole.SCRIBE)
        if selection is None:
            raise SemanticResolutionError("no control model is configured for semantic resolution")
        prompt = """You canonicalize one semantic property/relation for an RPG temporal knowledge graph.
Choose an EXISTING semantic_type_id only when the observation expresses the same semantic slot as a
supplied candidate, not merely a related topic. Otherwise define NEW. Candidate labels are descriptive;
identity is the UUID. Do not invent an existing UUID.

A candidate with non-null `system_key` is engine-owned and collision-only. If the observation expresses
that same slot, you MUST choose that EXISTING UUID rather than define a duplicate NEW slot. Selecting a
system_key candidate does not grant permission to mutate it; the backend will discard the open semantic
observation because deterministic executor state owns that slot.

For NEW, provide a compact stable label, a precise semantic description, cardinality (single when only
one value/target may be current for a subject, multi when independent simultaneous values/targets are
valid), and an optional JSON value schema. Return only the structured decision."""
        data = await self._model_router.generate_json(
            self._llm_provider,
            selection,
            [
                ChatMessage(role="system", content=prompt),
                ChatMessage(
                    role="user",
                    content=json.dumps(
                        {
                            "kind": kind,
                            "subject_entity_id": str(subject_entity_id),
                            "semantic_description": semantic_description,
                            "observed_value": observed_value,
                            "cardinality_hint": cardinality_hint,
                            "candidates": [
                                candidate.model_dump(mode="json") for candidate in candidates
                            ],
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                ),
            ],
            max_tokens=500,
            temperature=0.0,
            response_model=SemanticTypeResolutionDecision,
        )
        decision = self.validate_semantic_type_decision(
            SemanticTypeResolutionDecision.model_validate(data),
            candidates,
            expected_kind=kind,
        )
        return decision, candidates

    async def _hydrate_system_keys(
        self,
        candidates: list[SemanticTypeResolutionCandidate],
    ) -> list[SemanticTypeResolutionCandidate]:
        if not candidates:
            return []
        ids = [str(candidate.semantic_type_id) for candidate in candidates]
        rows = (
            await self._protected_session.execute(
                select(SemanticType.id, SemanticType.system_key).where(SemanticType.id.in_(ids))
            )
        ).all()
        system_keys = {row.id: row.system_key for row in rows}
        return [
            candidate.model_copy(
                update={"system_key": system_keys.get(str(candidate.semantic_type_id))}
            )
            for candidate in candidates
        ]

    @staticmethod
    def protected_collision(
        decision: SemanticTypeResolutionDecision,
        candidates: list[SemanticTypeResolutionCandidate],
    ) -> SemanticTypeResolutionCandidate | None:
        if decision.decision != "existing" or decision.semantic_type_id is None:
            return None
        return next(
            (
                candidate
                for candidate in candidates
                if candidate.semantic_type_id == decision.semantic_type_id
                and candidate.system_key is not None
            ),
            None,
        )


__all__ = ["ProtectedAwareSemanticResolver"]
