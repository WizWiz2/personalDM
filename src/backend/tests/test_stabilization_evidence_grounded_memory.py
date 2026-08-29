from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.proposed_change import ChangeType
from app.providers.llm_provider import LLMProviderError
from app.services.actor_turn_authority_guard import (
    build_actor_segment_proposals,
    extract_actor_segment_proposals,
    segment_actor_response,
)


PUBLISHED = (
    "Бармен понижает голос. «Кто-то уже спрашивал про „Морскую Звезду“. "
    "Он назвался заказчиком и ушёл к седьмому причалу»."
)
EXACT = "Он назвался заказчиком и ушёл к седьмому причалу"


class FakeRouter:
    def __init__(self, segment_ids=None, error: Exception | None = None):
        self.segment_ids = segment_ids or []
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
        return {"segment_ids": self.segment_ids}


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


def _segment_id_containing(segments: list[str], fragment: str) -> int:
    return next(index for index, value in enumerate(segments, start=1) if fragment in value)


def test_proposition_is_backend_owned_published_segment_not_llm_text():
    actor_id = uuid4()
    player_id = uuid4()
    segments = segment_actor_response(PUBLISHED)
    segment_id = _segment_id_containing(segments, EXACT)

    proposals = build_actor_segment_proposals(
        segments,
        [segment_id],
        acting_character_id=actor_id,
        player_character_id=player_id,
    )

    assert len(proposals) == 1
    payload = proposals[0].payload
    assert proposals[0].change_type == ChangeType.KNOWLEDGE
    assert payload["proposition"] == segments[segment_id - 1]
    assert payload["_canon"]["evidence"] == segments[segment_id - 1]
    assert payload["_canon"]["segment_id"] == segment_id
    assert payload["source_character_id"] == str(actor_id)
    assert payload["recipient_id"] == str(player_id)
    assert payload["_canon"]["authority"] == "character_claim"


def test_model_cannot_invent_or_invert_claim_text_because_it_returns_only_ids():
    segments = segment_actor_response(PUBLISHED)
    proposals = build_actor_segment_proposals(
        segments,
        [999, -1],
        acting_character_id=uuid4(),
        player_character_id=uuid4(),
    )

    assert proposals == []
    assert all("ничего не говорил" not in item for item in segments)


def test_explicit_negative_statement_is_valid_when_published_segment_selected():
    actor_id = uuid4()
    player_id = uuid4()
    published = "Сторож качает головой. «Я не видел Ивана после полуночи»."
    segments = segment_actor_response(published)
    segment_id = _segment_id_containing(segments, "Я не видел Ивана после полуночи")

    proposals = build_actor_segment_proposals(
        segments,
        [segment_id],
        acting_character_id=actor_id,
        player_character_id=player_id,
    )

    assert len(proposals) == 1
    assert "не видел Ивана" in proposals[0].payload["proposition"]


@pytest.mark.asyncio
async def test_extractor_uses_one_semantic_call_and_model_returns_only_segment_ids():
    actor_id = uuid4()
    player_id = uuid4()
    segments = segment_actor_response(PUBLISHED)
    segment_id = _segment_id_containing(segments, EXACT)
    router = FakeRouter(segment_ids=[segment_id, 999])

    result = await extract_actor_segment_proposals(
        _scribe(router),
        campaign_id=uuid4(),
        assistant_content=PUBLISHED,
        acting_character_id=actor_id,
        player_character_id=player_id,
    )

    assert router.calls == 1
    assert len(result) == 1
    assert result[0].payload["proposition"] == segments[segment_id - 1]
    prompt = "\n".join(message.content for message in router.last_messages)
    assert "ACTOR CLAIM SEGMENT SELECTOR" in prompt
    assert "Не пиши и не исправляй текст" in prompt
    assert f"S{segment_id}:" in prompt


@pytest.mark.asyncio
async def test_extractor_failure_does_not_create_or_invent_memory():
    actor_id = uuid4()
    player_id = uuid4()
    router = FakeRouter(error=LLMProviderError("planned extractor failure"))

    result = await extract_actor_segment_proposals(
        _scribe(router),
        campaign_id=uuid4(),
        assistant_content=PUBLISHED,
        acting_character_id=actor_id,
        player_character_id=player_id,
    )

    assert result == []
    assert router.calls == 1


@pytest.mark.asyncio
async def test_silence_is_semantically_rejected_as_knowledge():
    actor_id = uuid4()
    player_id = uuid4()
    router = FakeRouter(segment_ids=[])

    result = await extract_actor_segment_proposals(
        _scribe(router),
        campaign_id=uuid4(),
        assistant_content="Бармен умолкает.",
        acting_character_id=actor_id,
        player_character_id=player_id,
    )

    assert result == []
    assert router.calls == 1
    prompt = "\n".join(message.content for message in router.last_messages)
    assert "Не выбирай жесты, эмоции" in prompt
    assert "Если фактических утверждений нет" in prompt
