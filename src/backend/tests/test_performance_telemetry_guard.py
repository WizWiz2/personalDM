from app.services.performance_telemetry_guard import (
    _duration_ms,
    _ollama_timing_usage,
    _render_report,
)


def test_duration_ms_converts_ollama_nanoseconds() -> None:
    assert _duration_ms(1_250_000_000) == 1250.0
    assert _duration_ms(0) == 0.0
    assert _duration_ms(-1) is None
    assert _duration_ms("1000") is None


def test_ollama_timing_usage_exposes_stage_durations_and_throughput() -> None:
    timings = _ollama_timing_usage(
        {
            "total_duration": 2_000_000_000,
            "load_duration": 250_000_000,
            "prompt_eval_duration": 500_000_000,
            "eval_duration": 1_000_000_000,
            "prompt_eval_count": 250,
            "eval_count": 40,
        }
    )

    assert timings == {
        "ollama_total_ms": 2000.0,
        "ollama_load_ms": 250.0,
        "ollama_prompt_eval_ms": 500.0,
        "ollama_eval_ms": 1000.0,
        "ollama_prompt_tps": 500.0,
        "ollama_generation_tps": 40.0,
    }


def test_ollama_timing_usage_tolerates_missing_fields() -> None:
    assert _ollama_timing_usage({}) == {}


def test_performance_report_groups_stage_and_model() -> None:
    report = _render_report(
        [
            {
                "stage": "planner:CoordinatedTurnPlan",
                "model": "qwen2.5:7b",
                "wall_ms": 2000.0,
                "ollama_total_ms": 1900.0,
                "ollama_load_ms": 600.0,
                "ollama_prompt_eval_ms": 500.0,
                "ollama_eval_ms": 800.0,
                "prompt_tokens": 250,
                "completion_tokens": 40,
                "attempt_count": 1,
                "status": "completed",
            },
            {
                "stage": "text_stream",
                "model": "gemma4:e4b",
                "wall_ms": 3000.0,
                "ollama_total_ms": 2900.0,
                "ollama_load_ms": 700.0,
                "ollama_prompt_eval_ms": 400.0,
                "ollama_eval_ms": 1800.0,
                "prompt_tokens": 200,
                "completion_tokens": 90,
                "attempt_count": 1,
                "status": "completed",
            },
        ]
    )

    assert "Calls: **2**" in report
    assert "Model load: **1.3s**" in report
    assert "planner:CoordinatedTurnPlan" in report
    assert "text_stream" in report
    assert "qwen2.5:7b" in report
    assert "gemma4:e4b" in report


def test_turn_summaries_do_not_inflate_model_call_totals() -> None:
    report = _render_report([
        {"kind": "structured", "stage": "planner:Intent", "model": "test",
         "wall_ms": 2000, "attempt_count": 1},
        {"kind": "turn_summary", "total_ms": 5000, "status": "failed",
         "unaccounted_ms": 3000, "logical_calls": 1, "retries": 0,
         "user_turn_id": "turn-1", "generation_run_id": "run-1"},
    ])
    assert "Calls: **1**" in report
    assert "Median: 5.0s; P95: 5.0s" in report
    assert "| turn-1 | run-1 | failed | 5.00s | 3.00s | 1 | 0 |" in report


def test_http_and_flush_records_do_not_become_extra_model_calls():
    records=[
        {'kind':'structured','stage':'planner:Intent','model':'test','wall_ms':3000},
        {'kind':'llm_call_start','operation_id':'call'},
        {'kind':'provider_request_start','http_request_id':'request-1'},
        {'kind':'provider_request_start','http_request_id':'unfinished'},
        {'kind':'provider_request','http_request_id':'request-1','reason':'intent','wall_ms':2500,
         'headers_ms':200,'first_event_ms':400,'first_text_ms':None,'body_ms':2300,'status':'cancelled'},
        {'kind':'telemetry_flush','trace_id':'trace-1','wall_ms':800,'resume_gap_ms':100,
         'server_observed_ms':4000,'usage_saved':True,'decisions_saved':False},
    ]
    report=_render_report(records)
    assert 'Calls: **1**' in report
    assert 'HTTP requests: 1; started without a finish: 1' in report
    assert '| request-1 | intent | cancelled | 2500 | 200 | 400 | unknown | 2300 |' in report
    assert '| trace-1 | 800 | 100 | 4000 | True | False |' in report
