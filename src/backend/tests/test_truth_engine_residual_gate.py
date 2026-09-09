from __future__ import annotations

import json
from uuid import UUID

import pytest

from app.models.truth_engine_residual import (
    RawSemanticResidualEnvelope,
    ResidualAtomDisposition,
    ResidualEntityMention,
    ResidualFluentObservation,
    ResidualRelationObservation,
    sanitize_semantic_residual,
)
from app.services.truth_engine_residual_gate import SemanticResidualDispositionGate


class StubRouter:
    def __init__(self, decisions):
        self.decisions = decisions
        self.payload = None

    async def resolve(self, *args, **kwargs):
        return object()

    async def generate_json(self, provider, selection, messages, **kwargs):
        if kwargs['response_model'].__name__ == 'AtomAdmissionEnvelope':
            payload = json.loads(messages[1].content)
            return {'admissions': [{
                'atom_key': atom['atom_key'], 'source_quote': 'The lamp comes on.',
                'proposition_entailed': True, 'arguments_faithful': True,
                'holds_after_turn': True, 'authority': 'world', 'represented_by_effect_id': None,
                'reason': 'Directly observed resulting light state, not a movement receipt.',
            } for atom in payload['atoms']]}
        self.payload = json.loads(messages[1].content)
        return {"decisions": self.decisions}


def _envelope():
    raw = RawSemanticResidualEnvelope(
        entities=[
            ResidualEntityMention(ref="martin", mention_text="Martin", entity_type="character"),
            ResidualEntityMention(ref="warehouse", mention_text="warehouse", entity_type="location"),
            ResidualEntityMention(ref="lamp", mention_text="lamp", entity_type="item"),
        ],
        fluents=[
            ResidualFluentObservation(
                subject_ref="martin",
                semantic_description="Martin's reported belief about ownership",
                value="North Company owns the warehouse",
                description="Martin says the warehouse belongs to North Company.",
            ),
            ResidualFluentObservation(
                subject_ref="lamp",
                semantic_description="current emitted light state",
                value="on",
                description="The lamp is visibly lit.",
            ),
        ],
        relations=[
            ResidualRelationObservation(
                subject_ref="martin",
                object_ref="warehouse",
                semantic_description="walked into",
                description="Martin walked into the warehouse.",
            )
        ],
    )
    return sanitize_semantic_residual(raw).envelope


@pytest.mark.asyncio
async def test_gate_can_only_filter_existing_backend_atoms():
    envelope = _envelope()
    belief_key = envelope.fluents[0].atom_key
    light_key = envelope.fluents[1].atom_key
    movement_key = envelope.relations[0].atom_key
    router = StubRouter(
        [
            {"atom_key": belief_key, "disposition": "epistemic"},
            {"atom_key": light_key, "disposition": "objective"},
            {"atom_key": movement_key, "disposition": "receipt_owned"},
            {"atom_key": "invented", "disposition": "objective"},
        ]
    )
    gate = SemanticResidualDispositionGate(
        session=None,
        model_router=router,
        llm_provider=object(),
    )

    result = await gate.classify(
        UUID(int=1),
        envelope=envelope,
        user_content="I ask Martin about the warehouse and switch on the lamp.",
        assistant_content="Martin answers. The lamp comes on.",
        structured_receipts=[{"event_type": "movement"}],
    )

    assert {item.atom_key for item in result.decisions} == {
        belief_key,
        light_key,
        movement_key,
    }
    dispositions = {item.atom_key: item.disposition for item in result.decisions}
    assert dispositions[belief_key] == "epistemic"
    assert dispositions[light_key] == "objective"
    assert dispositions[movement_key] == "receipt_owned"
    assert len(result.objective.fluents) == 1
    assert result.objective.fluents[0].atom_key == light_key
    assert result.objective.relations == []
    assert [entity.ref for entity in result.objective.entities] == ["lamp"]
    assert {atom["atom_key"] for atom in router.payload["atoms"]} == {
        belief_key,
        light_key,
        movement_key,
    }


def test_gate_fails_closed_for_missing_or_duplicated_decisions():
    allowed = {"a", "b", "c"}
    returned = [
        ResidualAtomDisposition(atom_key="a", disposition="objective"),
        ResidualAtomDisposition(atom_key="a", disposition="objective"),
        ResidualAtomDisposition(atom_key="b", disposition="epistemic"),
        ResidualAtomDisposition(atom_key="outside", disposition="objective"),
    ]

    normalized = SemanticResidualDispositionGate._normalize_decisions(returned, allowed)
    by_key = {item.atom_key: item.disposition for item in normalized}

    assert by_key == {
        "a": "unsupported",
        "b": "epistemic",
        "c": "unsupported",
    }


@pytest.mark.asyncio
async def test_objective_classification_without_admission_cannot_reach_writer():
    envelope = _envelope()
    class MissingAdmissionRouter(StubRouter):
        async def generate_json(self, provider, selection, messages, **kwargs):
            if kwargs['response_model'].__name__ == 'AtomAdmissionEnvelope':
                return {'admissions': []}
            return await super().generate_json(provider, selection, messages, **kwargs)

    router = MissingAdmissionRouter([
        {'atom_key': a.atom_key, 'disposition': 'objective'}
        for a in [*envelope.fluents, *envelope.relations]
    ])
    result = await SemanticResidualDispositionGate(
        None, model_router=router, llm_provider=object(),
    ).classify(UUID(int=1), envelope=envelope, user_content='', assistant_content='The lamp comes on.')
    assert not result.objective.entities
    assert not result.objective.fluents
    assert not result.objective.relations
    assert all(d.disposition == 'unsupported' for d in result.decisions)
