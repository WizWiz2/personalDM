from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.proposed_change import ChangeType
from app.providers.llm_provider import LLMProviderError
from app.services.actor_turn_authority_guard import (
    build_actor_evidence_proposals,
    extract_actor_evidence_proposals,
)


PUBLISHED = (
    "Бармен понижает голос. «Кто-то уже спрашивал про „Морскую Звезду“. "
    "Он назвался заказчиком и ушёл к седьмому причалу»."
)
EXACT = "Он назвался заказчиком и ушёл к седьмому причалу"


class FakeRouter:
    def __init__(self, spans=None, error: Exception | None = None):
        self.spans = spans or []
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
        return {"evidence_spans": self.spans}


class FakeEntities:
    async def get_character(self, entity_id):
        return SimpleNamespace(
            canonical_name="Бармен" if str(entity_id).endswith("1") else "Мария"
        )


def _scribe(router: FakeRouter):
    return SimpleNamespace(
        _model_router=router,
        _llm_provider=SimpleNamespace(),
        _entity_repo=FakeEntities(),
    )


def test_proposition_is_exact_published_evidence_not_second_llm_paraphrase():
    actor_id = uuid4()
    player_id = uuid4()
    proposals = build_actor_evidence_proposals(
        [EXACT],
        acting_character_id=actor_id,
        player_character_id=player_id,
        authoritative_text=PUBLISHED,
    )

    assert len(proposals) == 1
    payload = proposals[0].payload
    assert proposals[0].change_type == ChangeType.KNOWLEDGE
    assert payload["proposition"] == EXACT
    assert payload["_canon"]["evidence"] == EXACT
    assert payload["source_character_id"] == str(actor_id)
    assert payload["recipient_id"] == str(player_id)
    assert payload["_canon"]["authority"] == "character_claim"


def test_round22_polarity_inversion_cannot_be_built_without_published_span():
    actor_id = uuid4()
    player_id = uuid4()
    proposals = build_actor_evidence_proposals(
        ["Бармен ничего не говорил о заказчике."],
        acting_character_id=actor_id,
        player_character_id=player_id,
        authoritative_text=PUBLISHED,
    )

    assert proposals == []


def test_explicit_negative_statement_is_valid_when_npc_actually_said_it():
    actor_id = uuid4()
    player_id = uuid4()
    published = "Сторож качает головой. «Я не видел Ивана после полуночи»."
    evidence = "Я не видел Ивана после полуночи"

    proposals = build_actor_evidence_proposals(
        [evidence],
        acting_character_id=actor_id,
        player_character_id=player_id,
        authoritative_text=published,
    )

    assert len(proposals) == 1
    assert proposals[0].payload["proposition"] == evidence


@pytest.mark.asyncio
async def test_extractor_uses_one_semantic_call_and_backend_rejects_invented_span():
    actor_id = uuid4()
    player_id = uuid4()
    router = FakeRouter(
        spans=[
            EXACT,
            "Заказчик был в красном пальто.",
        ]
    )

    result = await extract_actor_evidence_proposals(
        _scribe(router),
        campaign_id=uuid4(),
        assistant_content=PUBLISHED,
        acting_character_id=actor_id,
        player_character_id=player_id,
    )

    assert router.calls == 1
    assert len(result) == 1
    assert result[0].payload["proposition"] == EXACT
    prompt = "\n".join(message.content for message in router.last_messages)
    assert "ACTOR EVIDENCE EXTRACTOR" in prompt
    assert "копируй его дословно" in prompt


@pytest.mark.asyncio
async def test_extractor_failure_does_not_create_or_invent_memory():
    actor_id = uuid4()
    player_id = uuid4()
    router = FakeRouter(error=LLMProviderError("planned extractor failure"))

    result = await extract_actor_evidence_proposals(
        _scribe(router),
        campaign_id=uuid4(),
        assistant_content=PUBLISHED,
        acting_character_id=actor_id,
        player_character_id=player_id,
    )

    assert result == []
    assert router.calls == 1


@pytest.mark.asyncio
async def test_silence_never_calls_extractor_or_creates_knowledge():
    actor_id = uuid4()
    player_id = uuid4()
    router = FakeRouter(spans=["Я видел Ивана вчера"])

    result = await extract_actor_evidence_proposals(
        _scribe(router),
        campaign_id=uuid4(),
        assistant_content="Бармен умолкает.",
        acting_character_id=actor_id,
        player_character_id=player_id,
    )

    assert result == []
    assert router.calls == 0
