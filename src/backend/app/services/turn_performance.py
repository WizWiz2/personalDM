"""Per-attempt wall time. Phase totals never masquerade as explained work."""
from __future__ import annotations

import math
import statistics
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from time import perf_counter
from uuid import uuid4

call_reason: ContextVar[str | None] = ContextVar('performance_reason', default=None)
operation_parent: ContextVar[str | None] = ContextVar('performance_parent', default=None)


@contextmanager
def reason_scope(reason):
    token = call_reason.set(reason)
    try:
        yield
    finally:
        call_reason.reset(token)


@dataclass
class PerformanceTrace:
    started: float = field(default_factory=perf_counter)
    trace_id: str = field(default_factory=lambda: str(uuid4()))
    intervals: list[dict] = field(default_factory=list)
    phases: list[tuple[str, float]] = field(default_factory=list)
    dropped_intervals: int = 0
    finished: float | None = None
    status: str = 'running'

    def __post_init__(self):
        self.phases.append(('context', self.started))

    def phase(self, name, *, now=None):
        self.phases.append((name, perf_counter() if now is None else now))

    def add_interval(self, category, name, start, end, **details):
        if len(self.intervals) >= 2000:
            self.dropped_intervals += 1
            return
        self.intervals.append({
            **details,
            'operation_id': details.get('operation_id', str(uuid4())),
            'parent_id': details.get('parent_id', operation_parent.get() or self.trace_id),
            'phase': self.phases[-1][0], 'category': category, 'name': name,
            'start_ms': max(0, (start-self.started)*1000),
            'end_ms': max(0, (end-self.started)*1000),
            'wall_ms': max(0, (end-start)*1000),
        })

    def snapshot(self, now=None, *, status='running') -> dict:
        end = self.finished if self.finished is not None else (perf_counter() if now is None else now)
        total = max(0, (end-self.started)*1000)
        foreground = [row for row in self.intervals if not row.get('background')]
        calls = [row for row in foreground if row['category'] == 'llm']
        events = []
        for row in foreground:
            events.extend([(min(total,row['start_ms']), 1, row['category']),
                           (min(total,row['end_ms']), -1, row['category'])])
        active, exclusive = Counter(), Counter()
        previous = 0
        priorities = {'wait':0, 'database':1, 'local':2, 'llm':3}
        for point, delta, category in sorted(events):
            covered = [kind for kind, count in active.items() if count > 0]
            if covered:
                exclusive[min(covered, key=lambda kind: priorities.get(kind,9))] += point-previous
            active[category] += delta
            previous = point
        accounted = sum(exclusive.values())
        phases = []
        for i, (name, start) in enumerate(self.phases):
            stop = self.phases[i+1][1] if i+1<len(self.phases) else end
            phases.append({'name':name, 'start_ms':round((start-self.started)*1000,3),
                           'wall_ms':round(max(0,stop-start)*1000,3)})
        return {
            'version':1, 'trace_id':self.trace_id,
            'status':self.status if self.finished is not None else status,
            'total_ms':round(total,3), 'accounted_ms':round(accounted,3),
            'unaccounted_ms':round(max(0,total-accounted),3),
            'exclusive_ms':{key:round(value,3) for key,value in exclusive.items() if value},
            'logical_calls':len(calls),
            'retries':sum(max(0,row.get('attempt_count',1)-1) for row in calls),
            'phases':phases, 'operations':[dict(row) for row in self.intervals],
            'dropped_intervals':self.dropped_intervals,
        }


def current_trace():
    from app.services.llm_usage_tracker import current_usage_context
    context = current_usage_context()
    return context.performance if context else None


def mark_phase(name):
    trace = current_trace()
    if trace:
        trace.phase(name)


def finish_trace():
    trace = current_trace()
    if trace and trace.finished is None:
        trace.finished = perf_counter()


def record_interval(category, name, start, end=None, **details):
    trace = current_trace()
    if trace:
        trace.add_interval(category,name,start,perf_counter() if end is None else end,
                           background=correlation().get('background',False), **details)


def correlation():
    from app.services.campaign_write_barrier import background_job
    from app.services.llm_usage_tracker import current_usage_context
    context = current_usage_context()
    if context is None:
        return {}
    return {'trace_id':context.performance.trace_id, 'campaign_id':str(context.campaign_id),
            'user_turn_id':str(context.user_turn_id),
            'generation_run_id':str(context.generation_run_id) if context.generation_run_id else None,
            'background':background_job.get() or context.assistant_turn_id is not None}


def series_summary(rows):
    totals = sorted(row['total_ms'] for row in rows)
    return {'attempts':len(rows), 'statuses':dict(Counter(row['status'] for row in rows)),
            'median_ms':statistics.median(totals) if totals else None,
            'p95_ms':totals[max(0,math.ceil(.95*len(totals))-1)] if totals else None,
            'runs_with_retries':sum(row.get('retries',0)>0 for row in rows)}


def install_sql_timing():
    from sqlalchemy import event
    from sqlalchemy.engine import Engine
    if not event.contains(Engine,'before_cursor_execute',_sql_start):
        event.listen(Engine,'before_cursor_execute',_sql_start)
        event.listen(Engine,'after_cursor_execute',_sql_end)
        event.listen(Engine,'handle_error',_sql_error)


def _sql_start(conn,cursor,statement,parameters,context,executemany):
    if current_trace():
        context._pdm_started = perf_counter()


def _sql_end(conn,cursor,statement,parameters,context,executemany):
    started = getattr(context,'_pdm_started',None)
    if started is not None:
        record_interval('database',statement.split(None,1)[0] if statement else 'SQL',started)
        del context._pdm_started


def _sql_error(exception_context):
    context = exception_context.execution_context
    started = getattr(context,'_pdm_started',None)
    if started is not None:
        record_interval('database','SQL failed',started,
                        error=type(exception_context.original_exception).__name__)
        del context._pdm_started
