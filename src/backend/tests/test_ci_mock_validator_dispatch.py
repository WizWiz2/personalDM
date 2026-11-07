"""The CI stand-in must explicitly answer the production validator wire contract."""
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.item_state import ItemStateSnapshot
from app.models.turn import ChatMessage
from app.models.turn_authority import TurnAuthority
from app.providers.llm_provider import LLMProvider
from app.services.narration_publication_guard import NarrationPublicationGuard
from app.services.turn_authority_validator import TurnAuthorityValidator, findings_model
from tests import run_ci_mock_simulation_v3 as simulation

pytestmark = pytest.mark.interagent_contract_enforced


@pytest.mark.asyncio
async def test_ci_provider_returns_explicit_findings_from_typed_wire_without_prompt_marker(monkeypatch):
    monkeypatch.setattr(simulation, "CALLS", simulation.Counter())
    authority = TurnAuthority(campaign_id=uuid4(), trigger_turn_id=uuid4(), player_input="Смотрю.")
    candidate = "На стене видна отметка."
    wire = findings_model(authority, candidate)
    data = await simulation.deterministic_generate_json(
        LLMProvider(), [ChatMessage(role="system", content="Opaque validator instructions")],
        SimpleNamespace(), response_model=wire,
    )
    findings = wire.model_validate(data)
    assert findings.model_fields_set == {"violations", "names_given"}
    assert findings.violations == []
    assert findings.names_given == []
    assert simulation.CALLS["authority_validator"] == 1


@pytest.mark.asyncio
async def test_ci_provider_findings_are_accepted_by_real_validator_and_publication_guard(monkeypatch):
    monkeypatch.setattr(simulation, "CALLS", simulation.Counter())
    authority = TurnAuthority(campaign_id=uuid4(), trigger_turn_id=uuid4(), player_input="Смотрю.")
    candidate = "На стене видна отметка."
    provider = LLMProvider()

    async def generate_json(_provider, _selection, messages, **kwargs):
        return await simulation.deterministic_generate_json(provider, messages, SimpleNamespace(), **kwargs)

    result = await TurnAuthorityValidator(SimpleNamespace(generate_json=generate_json)).validate(
        SimpleNamespace(), authority, candidate,
    )
    published, audit = NarrationPublicationGuard.publish(authority, candidate, result)
    assert result.verdict == "pass"
    assert result.violations == []
    assert published == candidate
    assert audit["mode"] == "validated_candidate"
    assert simulation.CALLS["authority_validator"] == 1


@pytest.mark.asyncio
async def test_ci_validator_keeps_refusing_uncontrolled_item_position_prose(monkeypatch):
    monkeypatch.setattr(simulation, "CALLS", simulation.Counter())
    authority = TurnAuthority(
        campaign_id=uuid4(), trigger_turn_id=uuid4(), player_input="Смотрю.",
        item_states=[ItemStateSnapshot(item_id=uuid4(), name="Монета", owner_id=uuid4())],
    )
    candidate = "Монета лежит на стойке."
    wire = findings_model(authority, candidate)
    messages = [ChatMessage(role="user", content=(
        "[TURN FACTS]\n" + json.dumps(authority.validator_payload(), ensure_ascii=False)
        + "\n\n[CANDIDATE NARRATION]\n" + candidate
    ))]
    with pytest.raises(AssertionError, match="Mock inventory auditor does not support positional prose"):
        await simulation.deterministic_generate_json(
            LLMProvider(), messages, SimpleNamespace(), response_model=wire,
        )
