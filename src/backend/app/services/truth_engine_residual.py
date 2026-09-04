from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.provider_config_repo import ProviderConfigRepository
from app.models.entity import EntityType
from app.models.truth_engine import (
    EntityMentionObservation,
    FluentObservation,
    RelationObservation,
)
from app.models.truth_engine_residual import SemanticResidualEnvelope
from app.models.turn import ChatMessage
from app.providers.llm_provider import LLMProvider
from app.services.role_model_router import ModelRole, RoleModelRouter
from app.services.truth_engine_semantics import SemanticObservationCompiler


@dataclass(frozen=True)
class ResidualCompilationResult:
    entity_ids: dict[str, UUID]
    fluent_event_ids: tuple[UUID, ...]
    relation_event_ids: tuple[UUID, ...]


class SemanticResidualExtractor:
    """Extract semantic observations without producing persistence operations.

    This replaces the old Scribe ownership boundary: the model describes objective observations and
    local references only. It cannot invent database IDs, choose Fact/Relationship tables, or decide
    assert/revise/retract persistence operations.
    """

    SYSTEM_PROMPT = """[TE2 SEMANTIC RESIDUAL EXTRACTOR]
Extract only objective world information established by this completed RPG turn that is NOT already
represented by the supplied STRUCTURED RECEIPTS.

Return one structured SemanticResidualEnvelope.

Your output is an observation graph, not a database mutation plan:
- `entities` declares local references used by the other atoms. `ref` is a short local token only.
- `fluents` are state/property observations about one entity. Describe the semantic slot in ordinary
  language and provide the observed value. Do not invent subject/predicate database keys.
- `relations` are entity-to-entity relations. `present=false` means the same relation is explicitly
  established as no longer holding in this turn.
- Use only coarse entity_type values from ALLOWED ENTITY TYPES.

Do NOT emit:
- player intentions that were not established as outcomes;
- physical movement, item ownership/placement, time/focus transitions, or other facts already covered
  by STRUCTURED RECEIPTS;
- dialogue claims, rumours, suspicions or beliefs as objective truth;
- goals, plot hooks, scene theses, mood, prose colour or presentation detail;
- persistence concepts such as FACT, RELATIONSHIP table, assert, revise, retract, supersede, SQL, IDs.

Every fluent/relation ref must point to exactly one entity declared in `entities`. Mention text is only
linguistic evidence; stable identity is resolved later by another component. Prefer no atom over an
unsupported inference. Evidence should quote or tightly paraphrase the exact turn evidence, not explain
your reasoning."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        model_router: RoleModelRouter | None = None,
        llm_provider: LLMProvider | None = None,
    ):
        self._model_router = model_router or RoleModelRouter(ProviderConfigRepository(session))
        self._llm_provider = llm_provider or LLMProvider()

    async def extract(
        self,
        campaign_id: UUID,
        *,
        user_content: str,
        assistant_content: str,
        structured_receipts: list[dict] | None = None,
    ) -> SemanticResidualEnvelope:
        if not user_content.strip() and not assistant_content.strip():
            return SemanticResidualEnvelope()
        selection = await self._model_router.resolve(campaign_id, ModelRole.SCRIBE)
        if selection is None:
            return SemanticResidualEnvelope()

        payload = {
            "allowed_entity_types": [entity_type.value for entity_type in EntityType],
            "structured_receipts": structured_receipts or [],
            "player_input": user_content,
            "completed_narration": assistant_content,
        }
        data = await self._model_router.generate_json(
            self._llm_provider,
            selection,
            [
                ChatMessage(role="system", content=self.SYSTEM_PROMPT),
                ChatMessage(
                    role="user",
                    content=json.dumps(payload, ensure_ascii=False, indent=2),
                ),
            ],
            max_tokens=1800,
            temperature=0.0,
            response_model=SemanticResidualEnvelope,
        )
        return SemanticResidualEnvelope.model_validate(data)


class SemanticResidualCompiler:
    """Resolve a local residual graph into stable TE2 identities and canonical events."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        observation_compiler: SemanticObservationCompiler | None = None,
    ):
        self._observation_compiler = observation_compiler or SemanticObservationCompiler(session)

    async def compile(
        self,
        campaign_id: UUID,
        *,
        source_key: str,
        source_turn_id: UUID | None,
        scene_id: UUID | None,
        envelope: SemanticResidualEnvelope,
    ) -> ResidualCompilationResult:
        source_key = source_key.strip()
        if not source_key:
            raise ValueError("semantic residual compile requires a stable source_key")

        entity_ids: dict[str, UUID] = {}
        for entity in envelope.entities:
            entity_ids[entity.ref] = await self._observation_compiler.compile_entity_reference(
                campaign_id,
                EntityMentionObservation(
                    observation_key=f"{source_key}:entity:{entity.ref}",
                    mention_text=entity.mention_text,
                    entity_type=entity.entity_type,
                    description=entity.description,
                    source_turn_id=source_turn_id,
                    scene_id=scene_id,
                    mention_kind=entity.mention_kind,
                    evidence=entity.evidence,
                    context_entity_ids=list(entity_ids.values()),
                ),
            )

        fluent_event_ids: list[UUID] = []
        for atom in envelope.fluents:
            result = await self._observation_compiler.compile_fluent(
                campaign_id,
                FluentObservation(
                    observation_key=f"{source_key}:fluent:{atom.atom_key}",
                    subject_entity_id=entity_ids[atom.subject_ref],
                    semantic_description=atom.semantic_description,
                    value=atom.value,
                    description=atom.description,
                    source_turn_id=source_turn_id,
                    scene_id=scene_id,
                    authority="semantic_compiler",
                    evidence=atom.evidence,
                    cardinality_hint=atom.cardinality_hint,
                ),
            )
            fluent_event_ids.append(result.event_id)

        relation_event_ids: list[UUID] = []
        for atom in envelope.relations:
            result = await self._observation_compiler.compile_relation(
                campaign_id,
                RelationObservation(
                    observation_key=f"{source_key}:relation:{atom.atom_key}",
                    subject_entity_id=entity_ids[atom.subject_ref],
                    object_entity_id=entity_ids[atom.object_ref],
                    semantic_description=atom.semantic_description,
                    present=atom.present,
                    description=atom.description,
                    source_turn_id=source_turn_id,
                    authority="semantic_compiler",
                    evidence=atom.evidence,
                    cardinality_hint=atom.cardinality_hint,
                ),
            )
            relation_event_ids.append(result.event_id)

        return ResidualCompilationResult(
            entity_ids=entity_ids,
            fluent_event_ids=tuple(fluent_event_ids),
            relation_event_ids=tuple(relation_event_ids),
        )


__all__ = [
    "ResidualCompilationResult",
    "SemanticResidualCompiler",
    "SemanticResidualExtractor",
]
