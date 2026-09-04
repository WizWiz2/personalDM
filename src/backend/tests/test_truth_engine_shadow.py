from __future__ import annotations

import json
from uuid import UUID

import pytest
from sqlalchemy import func, select

from app.db.tables import Campaign, Turn
from app.db.truth_engine_table import (
    FluentAssertion,
    SemanticType,
    TruthEventRecord,
    WorldRelationAssertion,
)
from app.models.truth_engine import CanonicalEventCreate
from app.models.truth_engine_residual import (
    ResidualAtomDisposition,
    ResidualClassificationResult,
    ResidualEntityMention,
    ResidualFluentObservation,
    SemanticResidualEnvelope,
    objective_residual,
)
from app.services.truth_engine import WorldReducer
from app.services.truth_engine_shadow import SemanticResidualShadowService


class StubExtractor:
    def __init__(self, envelope: SemanticResidualEnvelope):
        self.envelope = envelope
        self.calls: list[dict] = []

    async def extract(self, campaign_id, **kwargs):
        self.calls.append({"campaign_id": campaign_id, **kwargs})
        return self.envelope


class StubClassifier:
    def __init__(self):
        self.calls: list[dict] = []

    async def classify(self, campaign_id, *, envelope, **kwargs):
        self.calls.append({"campaign_id": campaign_id, "envelope": envelope, **kwargs})
        decisions = [
            ResidualAtomDisposition(atom_key=atom.atom_key, disposition="objective")
            for atom in [*envelope.fluents, *envelope.relations]
        ]
        return ResidualClassificationResult(
            decisions=decisions,
            objective=objective_residual(envelope, decisions),
        )


@pytest.mark.asyncio
async def test_shadow_capture_uses_executor_receipts_and_does_not_mutate_te2_world(db_session):
    campaign = Campaign(name="TE2 shadow")
    db_session.add(campaign)
    await db_session.flush()
    campaign_id = UUID(campaign.id)
    campaign_id_text = str(campaign_id)
    user = Turn(
        campaign_id=campaign_id_text,
        role="user",
        content="I take the key and ask the watcher about the mark.",
        status="active",
    )
    db_session.add(user)
    await db_session.flush()
    user_id = UUID(user.id)
    assistant = Turn(
        campaign_id=campaign_id_text,
        role="assistant",
        content="You take the key. The watcher reveals that the mark is fresh.",
        parent_turn_id=user.id,
        status="active",
        context_snapshot=json.dumps({"existing": "metadata"}),
    )
    db_session.add(assistant)
    await db_session.flush()
    assistant_id = UUID(assistant.id)

    receipt = CanonicalEventCreate(
        event_key="receipt:test:key-take",
        event_type="item_transfer",
        description="The structured executor transferred the key.",
        source_kind="executor_receipt",
        source_turn_id=user_id,
        payload={
            "operation": "take",
            "item_id": "machine-key-id",
        },
    )
    await WorldReducer(db_session).append_and_reduce(campaign_id, receipt)
    await db_session.commit()

    envelope = SemanticResidualEnvelope(
        entities=[
            ResidualEntityMention(
                ref="watcher",
                mention_text="the watcher",
                entity_type="character",
            )
        ],
        fluents=[
            ResidualFluentObservation(
                atom_key="mark-age",
                subject_ref="watcher",
                semantic_description="the currently established assessment of the mark's age",
                value="fresh",
                description="The mark is established as fresh.",
            )
        ],
    )
    extractor = StubExtractor(envelope)
    classifier = StubClassifier()
    captured = await SemanticResidualShadowService(
        db_session,
        extractor=extractor,
        classifier=classifier,
    ).capture(assistant_id)
    await db_session.commit()

    assert captured is True
    assert len(extractor.calls) == 1
    expected_receipts = [
        {
            "event_id": str((await db_session.execute(
                select(TruthEventRecord.event_id).where(
                    TruthEventRecord.event_key == "receipt:test:key-take"
                )
            )).scalar_one()),
            "event_type": "item_transfer",
            "description": "The structured executor transferred the key.",
            "payload": {
                "item_id": "machine-key-id",
                "operation": "take",
            },
        }
    ]
    assert extractor.calls[0]["structured_receipts"] == expected_receipts
    assert len(classifier.calls) == 1
    assert classifier.calls[0]["structured_receipts"] == expected_receipts

    row = await db_session.get(Turn, str(assistant_id))
    snapshot = json.loads(row.context_snapshot)
    shadow = snapshot[SemanticResidualShadowService.SNAPSHOT_KEY]
    assert snapshot["existing"] == "metadata"
    assert shadow["mode"] == "read_only"
    assert shadow["version"] == 3
    assert shadow["receipt_count"] == 1
    assert shadow["sanitization"] == {
        "duplicate_entity_refs_dropped": 0,
        "dangling_fluents_dropped": 0,
        "dangling_relations_dropped": 0,
        "duplicate_fluents_dropped": 0,
        "duplicate_relations_dropped": 0,
    }
    assert shadow["counts"] == {
        "entities": 1,
        "fluents": 1,
        "relations": 0,
        "objective_entities": 1,
        "objective_fluents": 1,
        "objective_relations": 0,
    }
    assert shadow["dispositions"][0]["disposition"] == "objective"
    assert len(shadow["objective_residual"]["fluents"]) == 1

    # Shadow capture may update only diagnostic Turn metadata. It must not compile semantic state.
    assert (
        await db_session.execute(
            select(func.count(TruthEventRecord.event_id)).where(
                TruthEventRecord.campaign_id == campaign_id_text
            )
        )
    ).scalar_one() == 1
    assert (
        await db_session.execute(
            select(func.count(SemanticType.id)).where(
                SemanticType.campaign_id == campaign_id_text
            )
        )
    ).scalar_one() == 0
    assert (
        await db_session.execute(
            select(func.count(FluentAssertion.id)).where(
                FluentAssertion.campaign_id == campaign_id_text
            )
        )
    ).scalar_one() == 0
    assert (
        await db_session.execute(
            select(func.count(WorldRelationAssertion.id)).where(
                WorldRelationAssertion.campaign_id == campaign_id_text
            )
        )
    ).scalar_one() == 0


@pytest.mark.asyncio
async def test_shadow_capture_skips_inactive_source_pair_without_calling_model(db_session):
    campaign = Campaign(name="TE2 shadow inactive")
    db_session.add(campaign)
    await db_session.flush()
    campaign_id_text = campaign.id
    user = Turn(
        campaign_id=campaign_id_text,
        role="user",
        content="old input",
        status="reverted",
    )
    db_session.add(user)
    await db_session.flush()
    assistant = Turn(
        campaign_id=campaign_id_text,
        role="assistant",
        content="old output",
        parent_turn_id=user.id,
        status="alternative",
        context_snapshot="{}",
    )
    db_session.add(assistant)
    await db_session.flush()
    assistant_id = UUID(assistant.id)
    await db_session.commit()

    extractor = StubExtractor(SemanticResidualEnvelope())
    classifier = StubClassifier()
    captured = await SemanticResidualShadowService(
        db_session,
        extractor=extractor,
        classifier=classifier,
    ).capture(assistant_id)

    assert captured is False
    assert extractor.calls == []
    assert classifier.calls == []
    row = await db_session.get(Turn, str(assistant_id))
    assert SemanticResidualShadowService.SNAPSHOT_KEY not in json.loads(row.context_snapshot)
