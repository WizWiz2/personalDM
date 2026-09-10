from app.services.performance_telemetry_guard import _duration_ms, _ollama_timing_usage


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
