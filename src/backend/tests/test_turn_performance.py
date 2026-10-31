from app.services.turn_performance import PerformanceTrace, series_summary


def test_overlapping_calls_and_nested_wait_are_not_counted_twice():
    trace = PerformanceTrace(started=100)
    trace.add_interval('llm', 'outcomes', 100, 108)
    trace.add_interval('llm', 'parallel', 104, 109)
    trace.add_interval('wait', 'queue', 101, 103)
    trace.add_interval('database', 'UPDATE', 109, 110)
    trace.add_interval('llm', 'scribe', 100, 112, background=True)
    result = trace.snapshot(112)
    assert result['total_ms'] == 12000
    assert result['accounted_ms'] == 10000
    assert result['unaccounted_ms'] == 2000
    assert result['exclusive_ms'] == {'wait': 2000, 'database': 1000, 'llm': 7000}
    assert result['logical_calls'] == 2


def test_phases_do_not_hide_unknown_time_and_barrier_is_separate():
    trace = PerformanceTrace(started=10)
    trace.phase('planning', now=11)
    trace.phase('barrier', now=13)
    trace.add_interval('wait', 'campaign_barrier', 13, 15)
    trace.phase('preparation', now=15)
    result = trace.snapshot(16)
    assert [(p['name'], p['wall_ms']) for p in result['phases']] == [
        ('context', 1000), ('planning', 2000), ('barrier', 2000), ('preparation', 1000)]
    assert result['unaccounted_ms'] == 4000


def test_series_keeps_failures_and_retries():
    trace = PerformanceTrace(started=0)
    trace.add_interval('llm', 'outcomes', 0, 2, attempt_count=3)
    result = trace.snapshot(4, status='failed')
    assert result['retries'] == 2
    summary = series_summary([result, {**result, 'total_ms': 10000, 'status': 'published'}])
    assert summary['median_ms'] == 7000
    assert summary['p95_ms'] == 10000
    assert summary['runs_with_retries'] == 2
    assert summary['statuses'] == {'failed': 1, 'published': 1}
