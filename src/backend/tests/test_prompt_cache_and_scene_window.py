from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from app.providers.llm_provider import LLMProvider
from app.services.base_context_compiler import ContextCompiler


def test_prompt_cache_key_follows_the_stable_head_not_the_volatile_tail():
    config = SimpleNamespace(model_name="m")
    head = "R" * LLMProvider.PROMPT_CACHE_HEAD_CHARS
    first = LLMProvider._responses_payload(config, [{"role": "system", "content": head + "сцена 1"}])
    later = LLMProvider._responses_payload(config, [{"role": "system", "content": head + "сцена 9"}])
    other = LLMProvider._responses_payload(config, [{"role": "system", "content": "X" + head}])
    assert first["prompt_cache_key"] == later["prompt_cache_key"] != other["prompt_cache_key"]


def test_narrator_fact_window_does_not_grow_with_campaign_length():
    scene_id, here_id = uuid4(), uuid4()
    start = datetime(2026, 1, 1)
    old = [
        SimpleNamespace(id=uuid4(), scene_id=None, subject_entity_id=None, updated_at=start + timedelta(minutes=i))
        for i in range(300)
    ]
    local = SimpleNamespace(id=uuid4(), scene_id=scene_id, subject_entity_id=None, updated_at=start)
    person = SimpleNamespace(id=uuid4(), scene_id=None, subject_entity_id=here_id, updated_at=start)
    window = ContextCompiler._scene_window([*old, local, person], scene_id, {str(here_id)})
    assert local in window and person in window
    assert len(window) == 2 + ContextCompiler.SCENE_WINDOW_OTHER_FACTS
