import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.providers.llm_provider import LLMProvider
from app.services.memory_scribe import MemoryScribe


def test_extracts_openai_stream_delta():
    assert LLMProvider._extract_content(
        {"choices": [{"delta": {"content": "A violet spark"}}]}
    ) == "A violet spark"


def test_extracts_openai_message_response():
    assert LLMProvider._extract_content(
        {"choices": [{"message": {"content": "The gate opens."}}]}
    ) == "The gate opens."


def test_extracts_ollama_message_chunk():
    assert LLMProvider._extract_content(
        {"message": {"role": "assistant", "content": "Stone groans."}}
    ) == "Stone groans."


def test_extracts_ollama_generate_response():
    assert LLMProvider._extract_content({"response": "A cold wind rises."}) == (
        "A cold wind rises."
    )


def test_extracts_list_content_parts():
    assert LLMProvider._extract_content(
        {
            "choices": [
                {
                    "delta": {
                        "content": [
                            {"type": "text", "text": "One "},
                            {"type": "text", "text": "sentence."},
                        ]
                    }
                }
            ]
        }
    ) == "One sentence."


def test_extracts_finish_reason_and_usage_from_openai():
    frame = {
        "choices": [{"delta": {}, "finish_reason": "length"}],
        "usage": {
            "prompt_tokens": 4120,
            "completion_tokens": 512,
            "total_tokens": 4632,
        },
    }
    assert LLMProvider._extract_finish_reason(frame) == "length"
    assert LLMProvider._extract_usage(frame)["total_tokens"] == 4632


def test_extracts_finish_reason_and_usage_from_ollama():
    frame = {
        "done": True,
        "done_reason": "stop",
        "prompt_eval_count": 1200,
        "eval_count": 180,
    }
    assert LLMProvider._extract_finish_reason(frame) == "stop"
    assert LLMProvider._extract_usage(frame) == {
        "prompt_tokens": 1200,
        "completion_tokens": 180,
        "total_tokens": 1380,
    }


@pytest.mark.asyncio
async def test_scribe_ignores_empty_dm_result(db_session: AsyncSession):
    scribe = MemoryScribe(db_session)
    proposals = await scribe.extract_proposals(
        campaign_id=None,
        scene_id=None,
        user_content="I declare that the sealed gate opened.",
        assistant_content="   ",
    )
    assert proposals == []


def test_scribe_rejects_scene_thesis_from_fallback_parser():
    scribe = MemoryScribe(None)
    parsed = scribe._parse_response(
        '{"proposals":[{"change_type":"scene_thesis","payload":{"text":"stale"}}]}',
        {},
    )
    assert parsed == []
