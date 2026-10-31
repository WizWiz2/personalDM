from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.db.repositories.turn_repo import TurnRepository
from app.models.turn import TurnCreate
from app.providers.llm_provider import LLMProviderError
from app.services.authority_narration_pipeline import AuthorityNarrationPipeline
from app.services.narration_publication_guard import NarrationPublicationGuard
from app.services.narration_validator import NarrationValidationError
from app.services.turn_authority_validator import (
    InventoryNarrationValidation,
    TurnAuthorityValidator,
)
from tests.test_qa219_regressions import _world
from tests.test_qa220_regressions import _auth

pytestmark = pytest.mark.interagent_contract_enforced


def _reply(auth, claims=()):
    return {"position_claims": list(claims)}


BAD = {"position_claims": [{"item_id": "00000000-0000-0000-0000-000000000000", "evidence": "Осип",
                            "scope": "current", "mode": "located"}]}


@pytest.mark.asyncio
async def test_simple_mentions_need_no_redundant_quote_and_preserve_npc_answer():
    auth = _auth()
    text = "Осип кивает на ключевую доску. — Я ночной смотритель. А четвёртый номер заперт."
    router = SimpleNamespace(generate_json=AsyncMock(return_value=_reply(auth)))
    result = await TurnAuthorityValidator(router).validate(SimpleNamespace(), auth, text)
    assert NarrationPublicationGuard.publish(auth, text, result)[0] == text
    assert router.generate_json.await_count == 1
    schema = InventoryNarrationValidation.model_json_schema()
    assert "evidence" in schema["$defs"]["ItemPositionClaim"]["required"]


@pytest.mark.asyncio
async def test_invalid_validator_response_retries_same_prose_without_renarrating(db_session, monkeypatch):
    cid, _, _, _, _, _, scene = await _world(db_session)
    user = await TurnRepository(db_session).create(cid, TurnCreate(role="user", content="Что за четвёртый номер?", scene_id=scene))
    auth = _auth().model_copy(update={"campaign_id": cid, "trigger_turn_id": user.id})
    prose = "Осип придерживает связку. — Четвёртый номер закрыт с прошлой недели."
    router = SimpleNamespace(
        resolve=AsyncMock(return_value=SimpleNamespace(config=SimpleNamespace(model_name="test"))),
        generate_json=AsyncMock(side_effect=[BAD, _reply(auth)]),
    )
    pipeline = AuthorityNarrationPipeline(db_session, router)
    narrate = AsyncMock(return_value=(prose, None, {}))
    monkeypatch.setattr(pipeline, "_narrate", narrate)
    result = await pipeline.generate(campaign_id=cid, trigger_turn_id=user.id, scene_id=scene,
        narrator_messages=[], narrator_selection=SimpleNamespace(config=SimpleNamespace()), authority=auth)
    assert result.text == prose and result.validation_status == "passed"
    assert narrate.await_count == 1 and router.generate_json.await_count == 2
    retry = router.generate_json.call_args_list[1].args[2]
    assert prose in retry[1].content
    assert "[REPAIR VALIDATOR RESPONSE]" in retry[-1].content
    assert result.telemetry["narration_validation"]["publication_guard"]["mode"] == "validated_candidate"


@pytest.mark.asyncio
async def test_response_repair_cannot_hide_a_real_custody_conflict():
    auth = _auth()
    auth.observable_consequences = ["Ключ остаётся под контролем Осипа."]
    text = "Ключ от чердака лежит на конторке."
    claim = {"item_id": str(auth.item_states[0].item_id), "evidence": text,
             "scope": "current", "aspect": "placement", "mode": "located"}
    router = SimpleNamespace(generate_json=AsyncMock(side_effect=[
        _reply(auth, [{**claim, "evidence": "Нет такой цитаты"}]),
        _reply(auth, [claim]),
    ]))
    validator = TurnAuthorityValidator(router)
    result = await validator.validate(SimpleNamespace(), auth, text)
    assert result.verdict == "repair_required"
    assert NarrationPublicationGuard.publish(auth, text, result)[0] != text
    assert validator.telemetry["validation_response_retries"] == 1


@pytest.mark.asyncio
async def test_repeated_bad_response_is_bounded_and_does_not_certify_text():
    auth = _auth()
    router = SimpleNamespace(generate_json=AsyncMock(return_value=BAD))
    with pytest.raises(NarrationValidationError):
        await TurnAuthorityValidator(router).validate(SimpleNamespace(), auth, "Осип молчит.")
    assert router.generate_json.await_count == 2


@pytest.mark.asyncio
async def test_provider_outage_is_not_a_schema_retry_loop():
    router = SimpleNamespace(generate_json=AsyncMock(side_effect=LLMProviderError("offline")))
    with pytest.raises(NarrationValidationError, match="offline"):
        await TurnAuthorityValidator(router).validate(SimpleNamespace(), _auth(), "Осип молчит.")
    assert router.generate_json.await_count == 1
