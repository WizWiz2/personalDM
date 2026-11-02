import json
from uuid import uuid4

import httpx
import pytest

from app.providers.llm_provider import LLMProvider
from app.services.llm_usage_tracker import close_usage_context, current_usage_context, set_usage_context
from app.services.provider_request_timing import timed_client


class Clock:
    now = 0.0
    def __call__(self):
        return self.now


@pytest.mark.asyncio
async def test_headers_service_event_and_first_text_are_distinct(tmp_path, monkeypatch):
    from app.services import provider_request_timing as timing
    clock = Clock()
    monkeypatch.setattr(timing, 'perf_counter', clock)
    path = tmp_path / 'trace.jsonl'
    monkeypatch.setenv('PDM_PERFORMANCE_LOG', str(path))
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for at, data in [(0.03, ': keepalive'), (0.04, 'data: '+json.dumps({'type':'response.created'})),
                             (0.07, 'data: '+json.dumps({'type':'response.output_text.delta','delta':'hello'})),
                             (0.09, 'data: '+json.dumps({'type':'response.completed'}))]:
                clock.now = at
                yield (data+'\n\n').encode()
            clock.now = .1
    async def handle(request):
        clock.now = .02
        return httpx.Response(200, headers={'x-request-id':'safe-request-id'}, stream=Stream())
    token = set_usage_context(campaign_id=uuid4(),user_turn_id=uuid4(),performance_started=0)
    try:
        async with timed_client(httpx.AsyncClient(transport=httpx.MockTransport(handle))) as client:
            result = [item async for item in LLMProvider()._stream_once(
                client,'https://example.test/responses',{'Authorization':'Bearer SECRET'},
                {'model':'test','input':[{'role':'user','content':'PRIVATE'}],'stream':True})]
        assert result[-1]['type'] == 'response.completed'
        op = next(o for o in current_usage_context().performance.intervals if o['category']=='provider')
        assert op['headers_ms'] == 20
        assert op['first_event_ms'] == 40
        assert op['first_text_ms'] == 70
        assert op['body_ms'] == 80
        assert op['after_first_text_ms'] == 30
        assert op['provider_request_id'] == 'safe-request-id'
        assert op['parent_id'] == current_usage_context().performance.trace_id
    finally:
        await close_usage_context(token)
    raw = path.read_text()
    assert 'SECRET' not in raw and 'PRIVATE' not in raw
    records = [json.loads(line) for line in raw.splitlines()]
    assert [x['kind'] for x in records][:2] == ['provider_request_start','provider_request']


@pytest.mark.asyncio
async def test_buffered_response_has_header_and_body_times_but_no_fake_first_text(tmp_path, monkeypatch):
    from app.services import provider_request_timing as timing
    clock=Clock();monkeypatch.setattr(timing,'perf_counter',clock)
    monkeypatch.setenv('PDM_PERFORMANCE_LOG',str(tmp_path/'trace.jsonl'))
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            clock.now=.08
            yield b'{"ok":true}'
    async def handle(request):
        clock.now=.03
        return httpx.Response(200,stream=Stream())
    token=set_usage_context(campaign_id=uuid4(),user_turn_id=uuid4(),performance_started=0)
    try:
        async with timed_client(httpx.AsyncClient(transport=httpx.MockTransport(handle))) as client:
            assert (await client.post('https://example.test',json={'model':'test'})).json()=={'ok':True}
        op=next(o for o in current_usage_context().performance.intervals if o['category']=='provider')
        assert op['headers_ms']==30 and op['body_ms']==50
        assert op['first_text_ms'] is None
        assert op['mode']=='buffered'
    finally:
        await close_usage_context(token)
