from __future__ import annotations

import json
from collections import Counter
from typing import Iterable, Literal
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.provider_config_repo import ProviderConfigRepository
from app.db.tables import Entity, SceneParticipant
from app.db.truth_engine_table import (
    EntityMention,
    FluentAssertion,
    SemanticType,
    WorldRelationAssertion,
)
from app.models.truth_engine import (
    CanonicalEventCreate,
    EntityResolutionCandidate,
    EntityResolutionDecision,
    FluentObservation,
    RelationObservation,
    SemanticTypeCreate,
    SemanticTypeResolutionCandidate,
    SemanticTypeResolutionDecision,
    TruthEffectType,
    TruthEventEffectCreate,
    TruthEventEvidenceCreate,
    WorldReductionResult,
)
from app.models.turn import ChatMessage
from app.providers.llm_provider import LLMProvider, LLMProviderError
from app.services.role_model_router import ModelRole, RoleModelRouter
from app.services.truth_engine import SemanticTypeRegistry, WorldReducer


class SemanticResolutionError(RuntimeError):
    """A semantic decision violated the bounded candidate contract."""


class TruthCandidateRetriever:
    """Generate bounded semantic candidates from world structure, never word dictionaries.

    Candidate generation intentionally does not decide identity. It uses only machine-known
    structure: entity type, active scene membership, graph adjacency, prior mentions and currently
    active semantic slots. A later semantic judge may select one exact ID or NEW.
    """

    def __init__(self, session: AsyncSession):
        self._session = session

    async def entity_candidates(
        self,
        campaign_id: UUID,
        *,
        expected_types: Iterable[str] | None = None,
        scene_id: UUID | None = None,
        context_entity_ids: Iterable[UUID] = (),
        limit: int = 24,
    ) -> list[EntityResolutionCandidate]:
        if limit < 1:
            return []
        context_ids = {str(value) for value in context_entity_ids}
        scene_ids: set[str] = set()
        if scene_id is not None:
            scene_ids = set(
                (
                    await self._session.execute(
                        select(SceneParticipant.entity_id).where(
                            SceneParticipant.scene_id == str(scene_id)
                        )
                    )
                ).scalars().all()
            )

        linked_ids: set[str] = set(context_ids)
        if context_ids:
            relation_rows = list(
                (
                    await self._session.execute(
                        select(
                            WorldRelationAssertion.subject_entity_id,
                            WorldRelationAssertion.object_entity_id,
                        ).where(
                            WorldRelationAssertion.campaign_id == str(campaign_id),
                            WorldRelationAssertion.is_current.is_(True),
                            or_(
                                WorldRelationAssertion.subject_entity_id.in_(context_ids),
                                WorldRelationAssertion.object_entity_id.in_(context_ids),
                            ),
                        )
                    )
                ).all()
            )
            for subject_id, object_id in relation_rows:
                linked_ids.add(subject_id)
                linked_ids.add(object_id)

        mention_counts: Counter[str] = Counter()
        mention_rows = list(
            (
                await self._session.execute(
                    select(EntityMention.entity_id, func.count(EntityMention.id))
                    .where(
                        EntityMention.campaign_id == str(campaign_id),
                        EntityMention.entity_id.is_not(None),
                    )
                    .group_by(EntityMention.entity_id)
                )
            ).all()
        )
        for entity_id, count in mention_rows:
            if entity_id:
                mention_counts[entity_id] = int(count)

        query = select(Entity).where(
            Entity.campaign_id == str(campaign_id),
            Entity.status == "active",
        )
        type_values = tuple(dict.fromkeys(expected_types or ()))
        if type_values:
            query = query.where(Entity.entity_type.in_(type_values))
        rows = list((await self._session.execute(query)).scalars().all())

        rows.sort(
            key=lambda row: (
                0 if row.id in context_ids else 1,
                0 if row.id in scene_ids else 1,
                0 if row.id in linked_ids else 1,
                -mention_counts[row.id],
                row.created_at,
                row.id,
            )
        )
        return [
            EntityResolutionCandidate(
                entity_id=UUID(row.id),
                entity_type=row.entity_type,
                canonical_name=row.canonical_name,
                description=row.description,
                scene_local=row.id in scene_ids,
                context_linked=row.id in linked_ids,
                prior_mention_count=mention_counts[row.id],
            )
            for row in rows[:limit]
        ]

    async def semantic_type_candidates(
        self,
        campaign_id: UUID,
        *,
        kind: Literal["fluent", "relation"],
        subject_entity_id: UUID | None = None,
        include_system_types: bool = False,
        limit: int = 16,
    ) -> list[SemanticTypeResolutionCandidate]:
        if limit < 1:
            return []
        active_type_ids: set[str] = set()
        if subject_entity_id is not None:
            model = FluentAssertion if kind == "fluent" else WorldRelationAssertion
            active_type_ids = set(
                (
                    await self._session.execute(
                        select(model.semantic_type_id).where(
                            model.campaign_id == str(campaign_id),
                            model.subject_entity_id == str(subject_entity_id),
                            model.is_current.is_(True),
                        )
                    )
                ).scalars().all()
            )

        query = select(SemanticType).where(
            SemanticType.campaign_id == str(campaign_id),
            SemanticType.kind == kind,
        )
        if not include_system_types:
            query = query.where(SemanticType.system_key.is_(None))
        rows = list((await self._session.execute(query)).scalars().all())
        rows.sort(
            key=lambda row: (
                0 if row.id in active_type_ids else 1,
                row.created_at,
                row.id,
            )
        )
        return [
            SemanticTypeResolutionCandidate(
                semantic_type_id=UUID(row.id),
                kind=row.kind,
                canonical_label=row.canonical_label,
                description=row.description,
                cardinality=row.cardinality,
                value_schema=(json.loads(row.value_schema_json) if row.value_schema_json else None),
                active_for_subject=row.id in active_type_ids,
            )
            for row in rows[:limit]
        ]


class ConstrainedSemanticResolver:
    """Use an LLM only as a bounded semantic judge over stable candidate IDs."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        retriever: TruthCandidateRetriever | None = None,
        model_router: RoleModelRouter | None = None,
        llm_provider: LLMProvider | None = None,
    ):
        self._session = session
        self._retriever = retriever or TruthCandidateRetriever(session)
        self._model_router = model_router or RoleModelRouter(ProviderConfigRepository(session))
        self._llm_provider = llm_provider or LLMProvider()

    @staticmethod
    def validate_entity_decision(
        decision: EntityResolutionDecision,
        candidates: list[EntityResolutionCandidate],
    ) -> EntityResolutionDecision:
        if decision.decision == "new":
            return decision
        allowed = {candidate.entity_id for candidate in candidates}
        if decision.entity_id not in allowed:
            raise SemanticResolutionError("entity resolver selected an ID outside its candidates")
        return decision

    @staticmethod
    def validate_semantic_type_decision(
        decision: SemanticTypeResolutionDecision,
        candidates: list[SemanticTypeResolutionCandidate],
        *,
        expected_kind: str,
    ) -> SemanticTypeResolutionDecision:
        if decision.decision == "new":
            return decision
        allowed = {
            candidate.semantic_type_id
            for candidate in candidates
            if candidate.kind == expected_kind
        }
        if decision.semantic_type_id not in allowed:
            raise SemanticResolutionError(
                "semantic resolver selected an ID outside its candidates"
            )
        return decision

    async def resolve_entity(
        self,
        campaign_id: UUID,
        *,
        mention_text: str,
        expected_types: Iterable[str] | None = None,
        scene_id: UUID | None = None,
        context_entity_ids: Iterable[UUID] = (),
    ) -> EntityResolutionDecision:
        candidates = await self._retriever.entity_candidates(
            campaign_id,
            expected_types=expected_types,
            scene_id=scene_id,
            context_entity_ids=context_entity_ids,
        )
        if not candidates:
            return EntityResolutionDecision(decision="new")

        selection = await self._model_router.resolve(campaign_id, ModelRole.SCRIBE)
        if selection is None:
            raise SemanticResolutionError("no control model is configured for entity resolution")
        prompt = """You resolve one entity mention against a bounded candidate set for an RPG world model.
Identity is represented only by stable IDs. Decide whether the mention refers to exactly one supplied
candidate or to a genuinely new entity. Do not invent an ID and do not choose by superficial word
overlap alone; use the supplied scene/type/graph context. Return only the structured decision."""
        data = await self._model_router.generate_json(
            self._llm_provider,
            selection,
            [
                ChatMessage(role="system", content=prompt),
                ChatMessage(
                    role="user",
                    content=json.dumps(
                        {
                            "mention": mention_text,
                            "expected_types": list(expected_types or ()),
                            "scene_id": str(scene_id) if scene_id else None,
                            "candidates": [
                                candidate.model_dump(mode="json") for candidate in candidates
                            ],
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                ),
            ],
            max_tokens=250,
            temperature=0.0,
            response_model=EntityResolutionDecision,
        )
        return self.validate_entity_decision(
            EntityResolutionDecision.model_validate(data), candidates
        )

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
        )
        selection = await self._model_router.resolve(campaign_id, ModelRole.SCRIBE)
        if selection is None:
            raise SemanticResolutionError("no control model is configured for semantic resolution")
        prompt = """You canonicalize one semantic property/relation for an RPG temporal knowledge graph.
Choose an EXISTING semantic_type_id only when the observation expresses the same semantic slot as a
supplied candidate, not merely a related topic. Otherwise define NEW. Candidate labels are descriptive;
identity is the UUID. Do not invent an existing UUID. For NEW, provide a compact stable label, a precise
semantic description, cardinality (single when only one value/target may be current for a subject,
multi when independent simultaneous values/targets are valid), and an optional JSON value schema.
Return only the structured decision."""
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


class SemanticObservationCompiler:
    """Compile resolved semantic observations into the same TE2 event/effect protocol."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        resolver: ConstrainedSemanticResolver | None = None,
    ):
        self._session = session
        self._resolver = resolver or ConstrainedSemanticResolver(session)
        self._registry = SemanticTypeRegistry(session)
        self._reducer = WorldReducer(session)

    async def compile_fluent(
        self,
        campaign_id: UUID,
        observation: FluentObservation,
    ) -> WorldReductionResult:
        await self._validate_entity(campaign_id, observation.subject_entity_id)
        decision, _ = await self._resolver.resolve_semantic_type(
            campaign_id,
            kind="fluent",
            semantic_description=observation.semantic_description,
            subject_entity_id=observation.subject_entity_id,
            observed_value=observation.value,
            cardinality_hint=observation.cardinality_hint,
        )
        semantic_type_id, created_new = await self._materialize_type(
            campaign_id, "fluent", decision
        )
        event = CanonicalEventCreate(
            event_key=f"semantic_observation:{observation.observation_key}",
            event_type="semantic_state_observation",
            description=observation.description,
            source_kind="semantic_compiler",
            source_turn_id=observation.source_turn_id,
            participant_ids=[observation.subject_entity_id],
            payload={
                "semantic_type_id": str(semantic_type_id),
                "semantic_description": observation.semantic_description,
                "resolution": "new" if created_new else "existing",
            },
            effects=[
                TruthEventEffectCreate(
                    effect_type=TruthEffectType.SET_FLUENT,
                    payload={
                        "subject_entity_id": str(observation.subject_entity_id),
                        "semantic_type_id": str(semantic_type_id),
                        "value": observation.value,
                        "scene_id": str(observation.scene_id) if observation.scene_id else None,
                        "authority": observation.authority,
                    },
                )
            ],
            evidence=[
                TruthEventEvidenceCreate(
                    evidence_type="narrative_observation",
                    content=observation.evidence,
                    source_turn_id=observation.source_turn_id,
                    source_ref=f"semantic_observation:{observation.observation_key}",
                )
            ],
        )
        result = await self._reducer.append_and_reduce(campaign_id, event)
        if created_new:
            await self._attach_type_provenance(semantic_type_id, result.event_id)
        return result

    async def compile_relation(
        self,
        campaign_id: UUID,
        observation: RelationObservation,
    ) -> WorldReductionResult:
        await self._validate_entity(campaign_id, observation.subject_entity_id)
        await self._validate_entity(campaign_id, observation.object_entity_id)
        decision, _ = await self._resolver.resolve_semantic_type(
            campaign_id,
            kind="relation",
            semantic_description=observation.semantic_description,
            subject_entity_id=observation.subject_entity_id,
            observed_value={"object_entity_id": str(observation.object_entity_id)},
            cardinality_hint=observation.cardinality_hint,
        )
        semantic_type_id, created_new = await self._materialize_type(
            campaign_id, "relation", decision
        )
        event = CanonicalEventCreate(
            event_key=f"semantic_observation:{observation.observation_key}",
            event_type="semantic_relation_observation",
            description=observation.description,
            source_kind="semantic_compiler",
            source_turn_id=observation.source_turn_id,
            participant_ids=[observation.subject_entity_id, observation.object_entity_id],
            payload={
                "semantic_type_id": str(semantic_type_id),
                "semantic_description": observation.semantic_description,
                "resolution": "new" if created_new else "existing",
            },
            effects=[
                TruthEventEffectCreate(
                    effect_type=TruthEffectType.ADD_RELATION,
                    payload={
                        "subject_entity_id": str(observation.subject_entity_id),
                        "semantic_type_id": str(semantic_type_id),
                        "object_entity_id": str(observation.object_entity_id),
                        "authority": observation.authority,
                    },
                )
            ],
            evidence=[
                TruthEventEvidenceCreate(
                    evidence_type="narrative_observation",
                    content=observation.evidence,
                    source_turn_id=observation.source_turn_id,
                    source_ref=f"semantic_observation:{observation.observation_key}",
                )
            ],
        )
        result = await self._reducer.append_and_reduce(campaign_id, event)
        if created_new:
            await self._attach_type_provenance(semantic_type_id, result.event_id)
        return result

    async def _materialize_type(
        self,
        campaign_id: UUID,
        kind: Literal["fluent", "relation"],
        decision: SemanticTypeResolutionDecision,
    ) -> tuple[UUID, bool]:
        if decision.decision == "existing":
            semantic_type = await self._registry.get(decision.semantic_type_id)
            if semantic_type is None or semantic_type.campaign_id != str(campaign_id):
                raise SemanticResolutionError("resolved semantic type left the campaign boundary")
            if semantic_type.kind != kind:
                raise SemanticResolutionError("resolved semantic type changed semantic kind")
            return decision.semantic_type_id, False

        draft = decision.new_type
        if draft is None:
            raise SemanticResolutionError("new semantic decision is missing its type draft")
        semantic_type_id = await self._registry.create(
            campaign_id,
            SemanticTypeCreate(
                kind=kind,
                canonical_label=draft.canonical_label,
                description=draft.description,
                cardinality=draft.cardinality,
                value_schema=draft.value_schema,
            ),
        )
        return semantic_type_id, True

    async def _attach_type_provenance(self, semantic_type_id: UUID, event_id: UUID) -> None:
        semantic_type = await self._session.get(SemanticType, str(semantic_type_id))
        if semantic_type is not None and semantic_type.created_by_event_id is None:
            semantic_type.created_by_event_id = str(event_id)
            await self._session.flush()

    async def _validate_entity(self, campaign_id: UUID, entity_id: UUID) -> None:
        entity = await self._session.get(Entity, str(entity_id))
        if entity is None or entity.campaign_id != str(campaign_id) or entity.status != "active":
            raise SemanticResolutionError("semantic observation references an invalid entity")


__all__ = [
    "ConstrainedSemanticResolver",
    "SemanticObservationCompiler",
    "SemanticResolutionError",
    "TruthCandidateRetriever",
]
