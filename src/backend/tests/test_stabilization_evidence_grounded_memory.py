from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.proposed_change import ChangeType, ProposedChangeCreate
from app.providers.llm_provider import LLMProviderError
from app.services.actor_turn_authority_guard import (
    ActorClaim,
    build_actor_claim_proposals,
    filter_supported_actor_knowledge,
)


PUBLISHED = (
    "Бармен понижает голос. «Кто-то уже спрашивал про „Морскую Звезду“. "
    "Он назвался заказчиком и ушёл к седьмому причалу»."
)
EVIDENCE = "Кто-то уже спрашивал про „Морскую Звезду“. Он назвался заказчиком"


def _knowledge(
    *,
    source_id,
    recipient_id,
    proposition: str,
    evidence: str = EVIDENCE,
) -> ProposedChangeCreate:
    return ProposedChangeCreate(
        change_type=ChangeType.KNOWLEDGE,
        payload={
            "recipient_id": str(recipient_id),
            "source_character_id": str(source_id),
            "proposition": proposition,
            "confidence": 0.8,
            "status": "known",
            "_canon": {
                "kind": "knowledge_transfer",
                "evidence": evidence,
                "authority": "character_claim",
                "durable": True,
            },
        },
    )


class FakeRouter:
    def __init__(self, verdicts=None, error: Exception | None = None):
        self.verdicts = verdicts or []
        self.error = error
        self.calls = 0
        self.last_messages = None

    async def resolve(self, *args, **kwargs):
        return SimpleNamespace(config=SimpleNamespace(model_name="qwen2.5:7b"))

    async def generate_json(self, provider, selection, messages, **kwargs):
        self.calls += 1
        self.last_messages = messages
        if self.error:
            raise self.error
        return {"verdicts": self.verdicts}


def _scribe(router: FakeRouter):
    return SimpleNamespace(
        _model_router=router,
        _llm_provider=SimpleNamespace(),
    )


@pytest.mark.asyncio
async def test_round22_negative_inference_is_rejected_when_evidence_says_opposite():
    actor_id = uuid4()
    player_id = uuid4()
    bad = _knowledge(
        source_id=actor_id,
        recipient_id=player_id,
        proposition="Бармен ничего не говорил о заказчике.",
    )
    router = FakeRouter(verdicts=[{"index": 0, "supported": False}])

    result = await filter_supported_actor_knowledge(
        _scribe(router),
        [bad],
        campaign_id=uuid4(),
        assistant_content=PUBLISHED,
        acting_character_id=actor_id,
        player_character_id=player_id,
    )

    assert result == []
    assert router.calls == 1
    prompt = "\n".join(message.content for message in router.last_messages)
    assert "Бармен ничего не говорил о заказчике" in prompt
    assert EVIDENCE in prompt
    assert "Отрицательное утверждение поддерживается только явным отрицанием" in prompt


@pytest.mark.asyncio
async def test_supported_paraphrase_keeps_character_claim_not_objective_fact():
    actor_id = uuid4()
    player_id = uuid4()
    good = _knowledge(
        source_id=actor_id,
        recipient_id=player_id,
        proposition="Бармен сообщил, что человек назвался заказчиком.",
    )
    router = FakeRouter(verdicts=[{"index": 0, "supported": True}])

    result = await filter_supported_actor_knowledge(
        _scribe(router),
        [good],
        campaign_id=uuid4(),
        assistant_content=PUBLISHED,
        acting_character_id=actor_id,
        player_character_id=player_id,
    )

    assert result == [good]
    assert result[0].change_type == ChangeType.KNOWLEDGE
    assert result[0].payload["_canon"]["authority"] == "character_claim"


@pytest.mark.asyncio
async def test_wrong_speaker_or_non_extractable_evidence_fails_before_model_call():
    actor_id = uuid4()
    player_id = uuid4()
    other_id = uuid4()
    wrong_source = _knowledge(
        source_id=other_id,
        recipient_id=player_id,
        proposition="Кто-то назвался заказчиком.",
    )
    invented_evidence = _knowledge(
        source_id=actor_id,
        recipient_id=player_id,
        proposition="Заказчик был в красном пальто.",
        evidence="Заказчик был в красном пальто.",
    )
    router = FakeRouter(verdicts=[{"index": 0, "supported": True}])

    result = await filter_supported_actor_knowledge(
        _scribe(router),
        [wrong_source, invented_evidence],
        campaign_id=uuid4(),
        assistant_content=PUBLISHED,
        acting_character_id=actor_id,
        player_character_id=player_id,
    )

    assert result == []
    assert router.calls == 0


@pytest.mark.asyncio
async def test_entailment_transport_failure_fails_closed():
    actor_id = uuid4()
    player_id = uuid4()
    candidate = _knowledge(
        source_id=actor_id,
        recipient_id=player_id,
        proposition="Бармен сообщил, что человек назвался заказчиком.",
    )
    router = FakeRouter(error=LLMProviderError("planned verifier failure"))

    result = await filter_supported_actor_knowledge(
        _scribe(router),
        [candidate],
        campaign_id=uuid4(),
        assistant_content=PUBLISHED,
        acting_character_id=actor_id,
        player_character_id=player_id,
    )

    assert result == []
    assert router.calls == 1


def test_fallback_claim_keeps_exact_evidence_provenance_for_entailment():
    actor_id = uuid4()
    player_id = uuid4()
    proposals = build_actor_claim_proposals(
        [
            ActorClaim(
                proposition="Человек назвался заказчиком.",
                evidence=EVIDENCE,
            )
        ],
        acting_character_id=actor_id,
        player_character_id=player_id,
        authoritative_text=PUBLISHED,
    )

    assert len(proposals) == 1
    payload = proposals[0].payload
    assert payload["source_character_id"] == str(actor_id)
    assert payload["recipient_id"] == str(player_id)
    assert payload["_canon"]["evidence"] == EVIDENCE
    assert payload["_canon"]["authority"] == "character_claim"
