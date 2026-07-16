import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.providers.llm_provider import LLMProvider
from app.services.memory_scribe import MemoryScribe


def test_extracts_openai_stream_delta():
    assert (
        LLMProvider._extract_content(
            {"choices": [{"delta": {"content": "A violet spark"}}]}
        )
        == "A violet spark"
    )


def test_extracts_openai_message_response():
    assert (
        LLMProvider._extract_content(
            {"choices": [{"message": {"content": "The gate opens."}}]}
        )
        == "The gate opens."
    )


def test_extracts_ollama_message_chunk():
    assert (
        LLMProvider._extract_content(
            {"message": {"role": "assistant", "content": "Stone groans."}}
        )
        == "Stone groans."
    )


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
