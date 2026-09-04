from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.provider_config_repo import ProviderConfigRepository
from app.db.truth_engine_table import TruthEventRecord
from app.models.entity import EntityType
from app.models.truth_engine import (
    EntityBatchResolutionDecision,
    EntityMentionObservation,
    EntityResolutionDecision,
    FluentObservation,
    RelationObservation,
)
from app.models.truth_engine_residual import SemanticResidualEnvelope
from app.models.turn import ChatMessage
from app.providers.llm_provider import LLMProvider
from app.services.role_model_router import ModelRole, RoleModelRouter
from app.services.truth_engine import WorldReducer
from app.services.truth_engine_semantics import (
    ConstrainedSemanticResolver,
    SemanticObservationCompiler,
    SemanticResolutionError,
    TruthCandidateRetriever,
)


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
- Use one local entity ref consistently for all mentions of the same entity inside this envelope.
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

Player input is not objective authority by itself. A player-stated world claim belongs here only when
this completed turn independently establishes it as objective world state.

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


class JointResidualEntityResolver:
    """Resolve all unresolved local entity refs against one immutable candidate snapshot.

    Candidate retrieval happens for the entire batch before any entity/mention is materialized. The
    ambiguous subset is then judged in one bounded model call. Sorting by observation key makes the
    semantic input independent from arbitrary `entities[]` ordering in the extractor response.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        retriever: TruthCandidateRetriever | None = None,
        model_router: RoleModelRouter | None = None,
        llm_provider: LLMProvider | None = None,
    ):
        self._retriever = retriever or TruthCandidateRetriever(session)
        self._model_router = model_router or RoleModelRouter(ProviderConfigRepository(session))
        self._llm_provider = llm_provider or LLMProvider()

    async def resolve(
        self,
        campaign_id: UUID,
        observations: list[EntityMentionObservation],
        *,
        local_graph: dict | None = None,
    ) -> dict[str, EntityResolutionDecision]:
        if not observations:
            return {}

        ordered = sorted(observations, key=lambda item: item.observation_key)
        candidates_by_key: dict[str, list] = {}
        decisions: dict[str, EntityResolutionDecision] = {}
        ambiguous: list[EntityMentionObservation] = []

        # Complete candidate generation before any semantic decision can be materialized. All
        # observations therefore see the same world snapshot, independent of local-ref order.
        for observation in ordered:
            try:
                entity_type = EntityType(observation.entity_type).value
            except ValueError as exc:
                raise SemanticResolutionError(
                    f"unsupported entity type for semantic identity: {observation.entity_type}"
                ) from exc
            candidates = await self._retriever.entity_candidates(
                campaign_id,
                expected_types=[entity_type],
                scene_id=observation.scene_id,
                context_entity_ids=observation.context_entity_ids,
            )
            candidates_by_key[observation.observation_key] = candidates
            if candidates:
                ambiguous.append(observation)
            else:
                decisions[observation.observation_key] = EntityResolutionDecision(
                    decision="new"
                )

        if not ambiguous:
            return decisions

        selection = await self._model_router.resolve(campaign_id, ModelRole.SCRIBE)
        if selection is None:
            raise SemanticResolutionError("no control model is configured for entity batch resolution")

        prompt = """You jointly resolve entity mentions for one RPG turn against bounded candidate sets.
For every supplied observation, choose exactly one EXISTING candidate UUID or NEW. Candidate sets are
independent and authoritative: never select an ID absent from that observation's candidates and never
invent an existing ID. Evaluate the mentions together so local graph context can disambiguate identity,
but do not assume list order has meaning. The same existing candidate may be selected for multiple
local refs only when they clearly denote the same real entity. Do not use superficial word overlap as
identity evidence. Return exactly one structured item for every observation_key and no extra items."""

        payload_observations = []
        for observation in ambiguous:
            payload_observations.append(
                {
                    "observation_key": observation.observation_key,
                    "mention_text": observation.mention_text,
                    "entity_type": observation.entity_type,
                    "description": observation.description,
                    "evidence": observation.evidence,
                    "scene_id": str(observation.scene_id) if observation.scene_id else None,
                    "candidates": [
                        candidate.model_dump(mode="json")
                        for candidate in candidates_by_key[observation.observation_key]
                    ],
                }
            )

        data = await self._model_router.generate_json(
            self._llm_provider,
            selection,
            [
                ChatMessage(role="system", content=prompt),
                ChatMessage(
                    role="user",
                    content=json.dumps(
                        {
                            "observations": payload_observations,
                            "local_graph": local_graph or {},
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                ),
            ],
            max_tokens=max(350, 180 * len(ambiguous)),
            temperature=0.0,
            response_model=EntityBatchResolutionDecision,
        )
        batch = EntityBatchResolutionDecision.model_validate(data)
        expected_keys = {observation.observation_key for observation in ambiguous}
        actual_keys = {item.observation_key for item in batch.items}
        if actual_keys != expected_keys:
            missing = sorted(expected_keys - actual_keys)
            extra = sorted(actual_keys - expected_keys)
            raise SemanticResolutionError(
                "entity batch resolver returned the wrong observation set: "
                f"missing={missing}, extra={extra}"
            )

        for item in batch.items:
            candidates = candidates_by_key[item.observation_key]
            decisions[item.observation_key] = ConstrainedSemanticResolver.validate_entity_decision(
                item.resolution,
                candidates,
            )
        return decisions


class _PreResolvedEntityResolver:
    """One-use adapter that lets the existing compiler materialize a bounded batch decision."""

    def __init__(self, decision: EntityResolutionDecision):
        self._decision = decision

    async def resolve_entity(self, *args, **kwargs) -> EntityResolutionDecision:
        return self._decision

    async def resolve_semantic_type(self, *args, **kwargs):
        raise AssertionError("pre-resolved entity adapter cannot resolve semantic types")


class SemanticResidualCompiler:
    """Resolve a local residual graph into stable TE2 identities and canonical events.

    Delivery of one residual envelope is retry-idempotent. Entity decisions are prepared jointly
    before any local entity is materialized, so identity cannot depend on envelope ordering. Existing
    event keys are replayed before semantic resolution, preventing retries from re-running either
    entity alignment or semantic-type alignment.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        observation_compiler: SemanticObservationCompiler | None = None,
        entity_resolver: JointResidualEntityResolver | None = None,
    ):
        self._session = session
        self._observation_compiler = observation_compiler or SemanticObservationCompiler(session)
        self._entity_resolver = entity_resolver or JointResidualEntityResolver(session)
        self._reducer = WorldReducer(session)

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
        unresolved: list[tuple[str, EntityMentionObservation]] = []
        for entity in envelope.entities:
            observation = EntityMentionObservation(
                observation_key=f"{source_key}:entity:{entity.ref}",
                mention_text=entity.mention_text,
                entity_type=entity.entity_type,
                description=entity.description,
                source_turn_id=source_turn_id,
                scene_id=scene_id,
                mention_kind=entity.mention_kind,
                evidence=entity.evidence,
                context_entity_ids=[],
            )
            existing_id = await self._replay_existing_entity(campaign_id, observation.observation_key)
            if existing_id is not None:
                entity_ids[entity.ref] = existing_id
            else:
                unresolved.append((entity.ref, observation))

        if unresolved:
            local_graph = {
                "fluents": [
                    {
                        "atom_key": atom.atom_key,
                        "subject_ref": atom.subject_ref,
                        "semantic_description": atom.semantic_description,
                    }
                    for atom in sorted(envelope.fluents, key=lambda item: item.atom_key)
                ],
                "relations": [
                    {
                        "atom_key": atom.atom_key,
                        "subject_ref": atom.subject_ref,
                        "object_ref": atom.object_ref,
                        "semantic_description": atom.semantic_description,
                        "present": atom.present,
                    }
                    for atom in sorted(envelope.relations, key=lambda item: item.atom_key)
                ],
            }
            decisions = await self._entity_resolver.resolve(
                campaign_id,
                [observation for _, observation in unresolved],
                local_graph=local_graph,
            )

            # Materialization happens only after the complete batch is resolved and validated.
            for ref, observation in sorted(unresolved, key=lambda item: item[1].observation_key):
                decision = decisions.get(observation.observation_key)
                if decision is None:
                    raise SemanticResolutionError(
                        f"missing prepared entity decision for {observation.observation_key}"
                    )
                materializer = SemanticObservationCompiler(
                    self._session,
                    resolver=_PreResolvedEntityResolver(decision),
                )
                entity_ids[ref] = await materializer.compile_entity_reference(
                    campaign_id,
                    observation,
                )

        fluent_event_ids: list[UUID] = []
        for atom in envelope.fluents:
            observation_key = f"{source_key}:fluent:{atom.atom_key}"
            event_id = await self._replay_existing(
                campaign_id,
                f"semantic_observation:{observation_key}",
            )
            if event_id is None:
                result = await self._observation_compiler.compile_fluent(
                    campaign_id,
                    FluentObservation(
                        observation_key=observation_key,
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
                event_id = result.event_id
            fluent_event_ids.append(event_id)

        relation_event_ids: list[UUID] = []
        for atom in envelope.relations:
            observation_key = f"{source_key}:relation:{atom.atom_key}"
            event_id = await self._replay_existing(
                campaign_id,
                f"semantic_observation:{observation_key}",
            )
            if event_id is None:
                result = await self._observation_compiler.compile_relation(
                    campaign_id,
                    RelationObservation(
                        observation_key=observation_key,
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
                event_id = result.event_id
            relation_event_ids.append(event_id)

        return ResidualCompilationResult(
            entity_ids=entity_ids,
            fluent_event_ids=tuple(fluent_event_ids),
            relation_event_ids=tuple(relation_event_ids),
        )

    async def _replay_existing_entity(
        self,
        campaign_id: UUID,
        observation_key: str,
    ) -> UUID | None:
        record = (
            await self._session.execute(
                select(TruthEventRecord).where(
                    TruthEventRecord.campaign_id == str(campaign_id),
                    TruthEventRecord.event_key == f"semantic_entity:{observation_key}",
                )
            )
        ).scalar_one_or_none()
        if record is None:
            return None
        payload = json.loads(record.payload_json or "{}")
        raw_entity_id = payload.get("entity_id")
        if not raw_entity_id:
            raise SemanticResolutionError("existing entity observation lost its stable entity_id")
        await self._reducer.apply_event(UUID(record.event_id))
        return UUID(str(raw_entity_id))

    async def _replay_existing(self, campaign_id: UUID, event_key: str) -> UUID | None:
        record = (
            await self._session.execute(
                select(TruthEventRecord).where(
                    TruthEventRecord.campaign_id == str(campaign_id),
                    TruthEventRecord.event_key == event_key,
                )
            )
        ).scalar_one_or_none()
        if record is None:
            return None
        event_id = UUID(record.event_id)
        await self._reducer.apply_event(event_id)
        return event_id


__all__ = [
    "JointResidualEntityResolver",
    "ResidualCompilationResult",
    "SemanticResidualCompiler",
    "SemanticResidualExtractor",
]
