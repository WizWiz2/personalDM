from app.models.turn import ChatMessage
from app.providers.llm_provider import LLMProvider


def test_ollama_endpoint_detection():
    assert LLMProvider._is_ollama("http://127.0.0.1:11434/v1") is True
    assert LLMProvider._is_ollama("http://localhost:11434/v1") is True
    assert LLMProvider._is_ollama("https://api.example.com/v1") is False


def test_reasoning_is_counted_but_not_returned_as_content():
    frame = {
        "choices": [
            {
                "delta": {
                    "reasoning_content": "hidden chain",
                    "content": "visible answer",
                }
            }
        ]
    }
    assert LLMProvider._reasoning_characters(frame) == len("hidden chain")
    assert LLMProvider._extract_content(frame) == "visible answer"


def test_reasoning_only_frame_has_no_usable_content():
    frame = {
        "choices": [
            {
                "delta": {
                    "reasoning_content": "long hidden reasoning",
                },
                "finish_reason": "length",
            }
        ]
    }
    assert LLMProvider._reasoning_characters(frame) == len("long hidden reasoning")
    assert LLMProvider._extract_content(frame) == ""
    assert LLMProvider._extract_finish_reason(frame) == "length"


def test_completion_shape_detection():
    assert LLMProvider._looks_complete(
        "Гаррик показывает на западный овраг и велит группе держаться ниже гребня."
    ) is True
    assert LLMProvider._looks_complete(
        "Гаррик показывает на западный овраг и велит группе держаться"
    ) is False
    assert LLMProvider._looks_complete("коротко") is False


def test_json_prompt_detection_is_explicit():
    assert LLMProvider._expects_json(
        [ChatMessage(role="system", content="Верни только JSON с ключом proposals.")]
    ) is True
    assert LLMProvider._expects_json(
        [ChatMessage(role="system", content="Напиши художественный ответ на русском языке.")]
    ) is False


def test_balanced_json_parser_extracts_object_without_markdown_noise():
    assert LLMProvider._parse_json_object(
        'Пояснение до ответа {"proposals":[{"value":1}]} хвост'
    ) == {"proposals": [{"value": 1}]}


def test_balanced_json_parser_accepts_fenced_json():
    assert LLMProvider._parse_json_object(
        '```json\n{"desired_active":[]}\n```'
    ) == {"desired_active": []}
