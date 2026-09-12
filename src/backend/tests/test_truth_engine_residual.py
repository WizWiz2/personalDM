from __future__ import annotations

import json
from uuid import UUID

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.db.tables import Campaign, Entity
from app.db.truth_engine_table import (
    EntityMention,
    FluentAssertion,
    SemanticType,
    TruthEventRecord,
    WorldRelationAssertion,
)
from app.models.truth_engine import (
    EntityMentionObservation,
    EntityResolutionCandidate,
    EntityResolutionDecision,
    NewSemanticTypeDraft,
    SemanticTypeCreate,
    SemanticTypeResolutionDecision,
)
from app.models.truth_engine_residual import (
    ResidualEntityMention,
    ResidualFluentObservation,
    ResidualRelationObservation,
    SemanticResidualEnvelope,
)
from app.services.truth_engine import SemanticTypeRegistry
from app.services.truth_engine_residual import (
    JointResidualEntityResolver,
    SemanticResidualCompiler,
)
from app.services.truth_engine_semantics import SemanticObservationCompiler


class StubSemanticResolver:
    def __init__(self, *, semantic_decisions=()):
        self.semantic_decisions = list(semantic_decisions)
        self.semantic_calls = 0

    async def resolve_entity(self, *args, **kwargs):
        raise AssertionError("residual compiler must not use per-entity semantic resolution")

    async def resolve_semantic_type(self, *args, **kwargs):
        self.semantic_calls += 1
        if not self.semantic_decisions:
            raise AssertionError("unexpected semantic-type resolution call")
        return self.semantic_decisions.pop(0), []


class StubJointEntityResolver:
    def __init__(self, *decision_batches):
        self.decision_batches = [list(batch) for batch in decision_batches]
        self.calls = 0
        self.observation_orders: list[list[str]] = []

    async def resolve(self, campaign_id, observations, *, local_graph=None):
        self.calls += 1
        self.observation_orders.append([observation.observation_key for observation in observations])
        if not self.decision_batches:
            raise AssertionError("unexpected joint entity-resolution call")
        ordered = sorted(observations, key=lambda item: item.observation_key)
        decisions = self.decision_batches.pop(0)
        if len(decisions) != len(ordered):
            raise AssertionError("joint entity-resolution batch size mismatch")
        return {
            observation.observation_key: decision
            for observation, decision in zip(ordered, decisions, strict=True)
        }


class StubCandidateRetriever:
    def __init__(self, candidate: EntityResolutionCandidate):
        self.candidate = candidate
        self.calls: list[str] = []

    async def entity_candidates(self, campaign_id, **kwargs):
        self.calls.append(str(campaign_id))
        return [self.candidate]


class StubBatchModelRouter:
    def __init__(self):
        self.observation_keys: list[str] = []

    async def resolve(self, *args, **kwargs):
        return object()

    async def generate_json(self, provider, selection, messages, **kwargs):
        payload = json.loads(messages[1].content)
        self.observation_keys = [item["observation_key"] for item in payload["observations"]]
        return {
            "items": [
                {
                    "observation_key": key,
                    "resolution": {"decision": "existing", "entity_id": str(CANDIDATE_ID)},
                }
                for key in self.observation_keys
            ]
        }


CANDIDATE_ID = UUID("00000000-0000-0000-0000-000000000111")


async def _campaign(db_session) -> UUID:
    campaign = Campaign(name="TE2 residual")
    db_session.add(campaign)
    await db_session.flush()
    return UUID(campaign.id)


def _new_type(label: str, *, cardinality: str = "single") -> SemanticTypeResolutionDecision:
    return SemanticTypeResolutionDecision(
        decision="new",
        new_type=NewSemanticTypeDraft(
            canonical_label=label,
            description=f"Stable semantic slot for {label}.",
            cardinality=cardinality,
        ),
    )


def test_residual_envelope_rejects_dangling_local_refs():
    with pytest.raises(ValidationError, match="unknown subject_ref"):
        SemanticResidualEnvelope(
            entities=[
                ResidualEntityMention(
                    ref="keeper",
                    mention_text="keeper",
                    entity_type="character",
                )
            ],
            fluents=[
                ResidualFluentObservation(
                    atom_key="state",
                    subject_ref="missing",
                    semantic_description="current arbitrary state",
                    value="changed",
                    description="State changed.",
                )
            ],
        )


@pytest.mark.asyncio
async def test_joint_entity_resolver_sorts_batch_before_model_judgement():
    candidate = EntityResolutionCandidate(
        entity_id=CANDIDATE_ID,
        entity_type="character",
        canonical_name="Known person",
    )
    retriever = StubCandidateRetriever(candidate)
    router = StubBatchModelRouter()
    resolver = JointResidualEntityResolver(
        session=None,
        retriever=retriever,
        model_router=router,
        llm_provider=object(),
    )
    observations = [
        EntityMentionObservation(
            observation_key="turn:entity:zeta",
            mention_text="second mention",
            entity_type="character",
        ),
        EntityMentionObservation(
            observation_key="turn:entity:alpha",
            mention_text="first mention",
            entity_type="character",
        ),
    ]

    decisions = await resolver.resolve(UUID(int=1), observations)

    assert router.observation_keys == ["turn:entity:alpha", "turn:entity:zeta"]
    assert set(decisions) == {"turn:entity:alpha", "turn:entity:zeta"}
    assert all(decision.entity_id == CANDIDATE_ID for decision in decisions.values())


@pytest.mark.asyncio
async def test_residual_compiler_resolves_local_graph_to_stable_te2_state(db_session):
    campaign_id = await _campaign(db_session)
    semantic_resolver = StubSemanticResolver(
        semantic_decisions=[
            _new_type("Current stance"),
            _new_type("Current obligation target"),
        ]
    )
    entity_resolver = StubJointEntityResolver(
        [
            EntityResolutionDecision(decision="new"),
            EntityResolutionDecision(decision="new"),
        ]
    )
    compiler = SemanticResidualCompiler(
        db_session,
        observation_compiler=SemanticObservationCompiler(
            db_session,
            resolver=semantic_resolver,
        ),
        entity_resolver=entity_resolver,
    )
    envelope = SemanticResidualEnvelope(
        entities=[
            ResidualEntityMention(
                ref="watcher",
                mention_text="the watcher",
                entity_type="character",
                description="A newly established person in the scene.",
                evidence="The watcher steps forward.",
            ),
            ResidualEntityMention(
                ref="traveller",
                mention_text="the traveller",
                entity_type="character",
                description="Another established person.",
                evidence="The traveller answers.",
            ),
        ],
        fluents=[
            ResidualFluentObservation(
                atom_key="stance",
                subject_ref="watcher",
                semantic_description="the person's currently established social stance",
                value="friendly",
                description="The watcher is now openly friendly.",
                evidence="His manner turns openly friendly.",
            )
        ],
        relations=[
            ResidualRelationObservation(
                atom_key="obligation",
                subject_ref="traveller",
                object_ref="watcher",
                semantic_description="the current target of this person's explicit obligation",
                present=True,
                description="The traveller now owes the watcher a favour.",
                evidence="I owe you one, the traveller says, and the DM confirms the obligation.",
            )
        ],
    )

    result = await compiler.compile(
        campaign_id,
        source_key="turn-42",
        source_turn_id=None,
        scene_id=None,
        envelope=envelope,
    )

    assert entity_resolver.calls == 1
    assert set(result.entity_ids) == {"watcher", "traveller"}
    assert result.entity_ids["watcher"] != result.entity_ids["traveller"]
    assert len(result.fluent_event_ids) == 1
    assert len(result.relation_event_ids) == 1

    mentions = list(
        (
            await db_session.execute(
                select(EntityMention).where(EntityMention.campaign_id == str(campaign_id))
            )
        ).scalars().all()
    )
    fluent = (
        await db_session.execute(
            select(FluentAssertion).where(
                FluentAssertion.campaign_id == str(campaign_id),
                FluentAssertion.is_current.is_(True),
            )
        )
    ).scalar_one()
    relation = (
        await db_session.execute(
            select(WorldRelationAssertion).where(
                WorldRelationAssertion.campaign_id == str(campaign_id),
                WorldRelationAssertion.is_current.is_(True),
            )
        )
    ).scalar_one()

    assert len(mentions) == 2
    assert {mention.entity_id for mention in mentions} == {
        str(result.entity_ids["watcher"]),
        str(result.entity_ids["traveller"]),
    }
    assert fluent.subject_entity_id == str(result.entity_ids["watcher"])
    assert relation.subject_entity_id == str(result.entity_ids["traveller"])
    assert relation.object_entity_id == str(result.entity_ids["watcher"])


@pytest.mark.asyncio
async def test_residual_entity_materialization_is_independent_of_envelope_order(db_session):
    campaign_id = await _campaign(db_session)
    first_id = UUID("00000000-0000-0000-0000-000000000201")
    second_id = UUID("00000000-0000-0000-0000-000000000202")
    for entity_id, name in ((first_id, "First"), (second_id, "Second")):
        db_session.add(
            Entity(
                id=str(entity_id),
                campaign_id=str(campaign_id),
                entity_type="character",
                canonical_name=name,
            )
        )
    await db_session.flush()

    class MappingJointResolver:
        def __init__(self):
            self.calls = 0

        async def resolve(self, campaign_id, observations, *, local_graph=None):
            self.calls += 1
            return {
                observation.observation_key: EntityResolutionDecision(
                    decision="existing",
                    entity_id=(first_id if observation.observation_key.endswith(":a") else second_id),
                )
                for observation in observations
            }

    entity_resolver = MappingJointResolver()
    semantic_resolver = StubSemanticResolver()
    compiler = SemanticResidualCompiler(
        db_session,
        observation_compiler=SemanticObservationCompiler(
            db_session,
            resolver=semantic_resolver,
        ),
        entity_resolver=entity_resolver,
    )
    envelope = SemanticResidualEnvelope(
        entities=[
            ResidualEntityMention(ref="b", mention_text="Second", entity_type="character"),
            ResidualEntityMention(ref="a", mention_text="First", entity_type="character"),
        ]
    )

    result = await compiler.compile(
        campaign_id,
        source_key="order-proof",
        source_turn_id=None,
        scene_id=None,
        envelope=envelope,
    )

    assert entity_resolver.calls == 1
    assert result.entity_ids == {"a": first_id, "b": second_id}
    mentions = list(
        (
            await db_session.execute(
                select(EntityMention).where(EntityMention.campaign_id == str(campaign_id))
            )
        ).scalars().all()
    )
    assert [(mention.mention_text, mention.entity_id) for mention in mentions] == [
        ("First", str(first_id)),
        ("Second", str(second_id)),
    ]


@pytest.mark.asyncio
async def test_residual_retry_does_not_rerun_semantic_resolution_or_duplicate_schema(db_session):
    campaign_id = await _campaign(db_session)
    semantic_resolver = StubSemanticResolver(
        semantic_decisions=[
            _new_type("Current condition"),
            _new_type("Current affiliation", cardinality="multi"),
        ]
    )
    entity_resolver = StubJointEntityResolver(
        [
            EntityResolutionDecision(decision="new"),
            EntityResolutionDecision(decision="new"),
        ]
    )
    compiler = SemanticResidualCompiler(
        db_session,
        observation_compiler=SemanticObservationCompiler(
            db_session,
            resolver=semantic_resolver,
        ),
        entity_resolver=entity_resolver,
    )
    envelope = SemanticResidualEnvelope(
        entities=[
            ResidualEntityMention(ref="a", mention_text="guard", entity_type="character"),
            ResidualEntityMention(ref="b", mention_text="guard", entity_type="character"),
        ],
        fluents=[
            ResidualFluentObservation(
                atom_key="condition",
                subject_ref="a",
                semantic_description="current physical condition",
                value="tired",
                description="One guard is tired.",
            )
        ],
        relations=[
            ResidualRelationObservation(
                atom_key="affiliation",
                subject_ref="a",
                object_ref="b",
                semantic_description="current affiliation between these people",
                description="The guards are affiliated.",
            )
        ],
    )

    first = await compiler.compile(
        campaign_id,
        source_key="retryable-turn",
        source_turn_id=None,
        scene_id=None,
        envelope=envelope,
    )
    second = await compiler.compile(
        campaign_id,
        source_key="retryable-turn",
        source_turn_id=None,
        scene_id=None,
        envelope=envelope,
    )

    assert first == second
    assert entity_resolver.calls == 1
    assert semantic_resolver.semantic_calls == 2

    semantic_types = list(
        (
            await db_session.execute(
                select(SemanticType).where(SemanticType.campaign_id == str(campaign_id))
            )
        ).scalars().all()
    )
    events = list(
        (
            await db_session.execute(
                select(TruthEventRecord).where(TruthEventRecord.campaign_id == str(campaign_id))
            )
        ).scalars().all()
    )
    entities = list(
        (
            await db_session.execute(
                select(Entity).where(Entity.campaign_id == str(campaign_id))
            )
        ).scalars().all()
    )

    assert len(semantic_types) == 2
    assert len(events) == 4
    assert len(entities) == 2
    assert entities[0].canonical_name == entities[1].canonical_name == "guard"
    assert entities[0].id != entities[1].id


@pytest.mark.asyncio
async def test_residual_relation_absence_closes_temporal_relation_by_ids(db_session):
    campaign_id = await _campaign(db_session)
    left = Entity(
        campaign_id=str(campaign_id),
        entity_type="character",
        canonical_name="Left",
    )
    right = Entity(
        campaign_id=str(campaign_id),
        entity_type="character",
        canonical_name="Right",
    )
    db_session.add_all([left, right])
    await db_session.flush()
    left_id = UUID(left.id)
    right_id = UUID(right.id)

    relation_type_id = await SemanticTypeRegistry(db_session).create(
        campaign_id,
        SemanticTypeCreate(
            kind="relation",
            canonical_label="Explicit obligation",
            description="An explicit currently active obligation between two entities.",
            cardinality="multi",
        ),
    )
    existing_type = SemanticTypeResolutionDecision(
        decision="existing",
        semantic_type_id=relation_type_id,
    )
    semantic_resolver = StubSemanticResolver(
        semantic_decisions=[existing_type, existing_type],
    )
    entity_resolver = StubJointEntityResolver(
        [
            EntityResolutionDecision(decision="existing", entity_id=left_id),
            EntityResolutionDecision(decision="existing", entity_id=right_id),
        ],
        [
            EntityResolutionDecision(decision="existing", entity_id=left_id),
            EntityResolutionDecision(decision="existing", entity_id=right_id),
        ],
    )
    compiler = SemanticResidualCompiler(
        db_session,
        observation_compiler=SemanticObservationCompiler(
            db_session,
            resolver=semantic_resolver,
        ),
        entity_resolver=entity_resolver,
    )

    def envelope(*, present: bool, atom_key: str) -> SemanticResidualEnvelope:
        return SemanticResidualEnvelope(
            entities=[
                ResidualEntityMention(ref="left", mention_text="Left", entity_type="character"),
                ResidualEntityMention(ref="right", mention_text="Right", entity_type="character"),
            ],
            relations=[
                ResidualRelationObservation(
                    atom_key=atom_key,
                    subject_ref="left",
                    object_ref="right",
                    semantic_description="the explicit obligation currently connecting these entities",
                    present=present,
                    description=(
                        "The obligation now exists."
                        if present
                        else "The obligation is explicitly discharged."
                    ),
                )
            ],
        )

    created = await compiler.compile(
        campaign_id,
        source_key="obligation-created",
        source_turn_id=None,
        scene_id=None,
        envelope=envelope(present=True, atom_key="opened"),
    )
    closed = await compiler.compile(
        campaign_id,
        source_key="obligation-closed",
        source_turn_id=None,
        scene_id=None,
        envelope=envelope(present=False, atom_key="closed"),
    )

    relation = (
        await db_session.execute(
            select(WorldRelationAssertion).where(
                WorldRelationAssertion.campaign_id == str(campaign_id),
                WorldRelationAssertion.semantic_type_id == str(relation_type_id),
                WorldRelationAssertion.subject_entity_id == str(left_id),
                WorldRelationAssertion.object_entity_id == str(right_id),
            )
        )
    ).scalar_one()

    assert entity_resolver.calls == 2
    assert created.relation_event_ids
    assert closed.relation_event_ids
    assert relation.is_current is False
    assert relation.valid_until_event_id == str(closed.relation_event_ids[0])
