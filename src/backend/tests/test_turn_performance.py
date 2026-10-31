import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.services.llm_usage_tracker import (
    close_usage_context,
    current_usage_context,
    record_decision,
    set_usage_context,
)
from app.services.turn_performance import (
    PerformanceTrace,
    current_trace,
    install_sql_timing,
    reason_scope,
    series_summary,
)


def test_overlapping_calls_and_nested_wait_are_not_counted_twice():
    trace = PerformanceTrace(started=100)
    trace.add_interval("llm", "outcomes", 100, 108)
    trace.add_interval("llm", "parallel", 104, 109)
    trace.add_interval("wait", "queue", 101, 103)
    trace.add_interval("database", "UPDATE", 109, 110)
    trace.add_interval("llm", "scribe", 100, 112, background=True)
    result = trace.snapshot(112)
    assert result["total_ms"] == 12000
    assert result["accounted_ms"] == 10000
    assert result["unaccounted_ms"] == 2000
    assert result["exclusive_ms"] == {"wait": 2000, "database": 1000, "llm": 7000}
    assert result["logical_calls"] == 2


def test_phases_do_not_hide_unknown_time_and_barrier_is_separate():
    trace = PerformanceTrace(started=10)
    trace.phase("planning", now=11)
    trace.phase("barrier", now=13)
    trace.add_interval("wait", "campaign_barrier", 13, 15)
    trace.phase("preparation", now=15)
    result = trace.snapshot(16)
    assert [(p["name"], p["wall_ms"]) for p in result["phases"]] == [
        ("context", 1000),
        ("planning", 2000),
        ("barrier", 2000),
        ("preparation", 1000),
    ]
    assert result["unaccounted_ms"] == 4000


def test_series_keeps_failures_and_retries():
    trace = PerformanceTrace(started=0)
    trace.add_interval("llm", "outcomes", 0, 2, attempt_count=3)
    result = trace.snapshot(4, status="failed")
    assert result["retries"] == 2
    summary = series_summary([result, {**result, "total_ms": 10000, "status": "published"}])
    assert summary["median_ms"] == 7000
    assert summary["p95_ms"] == 10000
    assert summary["runs_with_retries"] == 2
    assert summary["statuses"] == {"failed": 1, "published": 1}


def usage_context(**kwargs):
    return set_usage_context(
        campaign_id=uuid4(), user_turn_id=uuid4(), generation_run_id=uuid4(), **kwargs
    )


@pytest.mark.asyncio
async def test_sqlite_contention_is_measured_by_two_real_engines(tmp_path):
    install_sql_timing()
    url = f"sqlite+aiosqlite:///{tmp_path / 'timing.db'}"
    writer, reader = create_async_engine(url), create_async_engine(url)
    async with writer.begin() as conn:
        await conn.execute(text("CREATE TABLE receipt (id INTEGER)"))
    token = usage_context()
    ready = asyncio.Event()
    try:
        async with writer.connect() as held:
            await held.execute(text("INSERT INTO receipt VALUES (1)"))

            async def competing():
                async with reader.begin() as conn:
                    ready.set()
                    await conn.execute(text("INSERT INTO receipt VALUES (2)"))

            task = asyncio.create_task(competing())
            await ready.wait()
            await asyncio.sleep(0.08)
            await held.commit()
            await task
        result = current_trace().snapshot()
        assert max(o["wall_ms"] for o in result["operations"] if o["category"] == "database") >= 60
        assert result["exclusive_ms"]["database"] >= 60
        async with reader.connect() as conn:
            assert await conn.scalar(text("SELECT COUNT(*) FROM receipt")) == 2
    finally:
        await close_usage_context(token)
        await writer.dispose()
        await reader.dispose()


@pytest.mark.asyncio
async def test_cancelled_request_retains_correlation_reason_and_partial_time(tmp_path, monkeypatch):
    from app.providers.llm_provider import LLMProvider
    from app.services.performance_telemetry_guard import install
    from app.services.role_model_router import ModelRole, RoleModelRouter

    install()
    path = tmp_path / "performance.jsonl"
    monkeypatch.setenv("PDM_PERFORMANCE_LOG", str(path))
    entered = asyncio.Event()

    async def wait(self, provider, *args, **kwargs):
        provider.last_telemetry = {"model": "test-model"}
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(RoleModelRouter, "_generate_json_once", wait)
    token = usage_context()
    context = current_usage_context()
    selection = SimpleNamespace(role=ModelRole.PLANNER, config=None, api_key=None)
    try:
        with reason_scope("repair_after_contract_error"):
            task = asyncio.create_task(
                RoleModelRouter(None).generate_json(LLMProvider(), selection, [])
            )
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        record_decision("final", "cancelled")
    finally:
        await close_usage_context(token)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    call = next(row for row in rows if row["kind"] == "structured")
    summary = next(row for row in rows if row["kind"] == "turn_summary")
    assert call["user_turn_id"] == str(context.user_turn_id)
    assert call["trace_id"] == summary["trace_id"]
    assert call["reason"] == "repair_after_contract_error"
    assert call["status"] == "cancelled"
    assert call["wall_ms"] > 0
    assert summary["status"] == "cancelled"
    assert summary["logical_calls"] == 1
    assert current_usage_context() is None


@pytest.mark.asyncio
async def test_background_context_is_independent_and_not_in_foreground_total():
    from app.services.campaign_write_barrier import background_job
    from app.services.turn_performance import record_interval

    token = usage_context()
    original = current_trace()
    try:
        flag = background_job.set(True)
        nested = usage_context(assistant_turn_id=uuid4())
        try:
            record_interval("llm", "scribe", original.started, original.started + 0.1)
            assert current_trace().snapshot()["logical_calls"] == 0
        finally:
            await close_usage_context(nested)
            background_job.reset(flag)
        assert current_trace() is original
        assert original.snapshot()["operations"] == []
    finally:
        await close_usage_context(token)
