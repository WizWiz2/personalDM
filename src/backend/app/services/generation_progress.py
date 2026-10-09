"""Live diagnostics scoped to one worker; durable lifecycle stays authoritative."""
from contextvars import ContextVar
from datetime import datetime, timezone

_current: ContextVar[str | None] = ContextVar("generation_progress_id", default=None)
_progress: dict[str, dict] = {}


def begin(run_id):
    key = str(run_id)
    token = _current.set(key)
    _progress[key] = {"stage": "preparing_context", "stage_started_at": datetime.now(timezone.utc),
                      "last_activity_at": None, "generated_chunks": 0, "model": None}
    return token


def finish(token):
    key = _current.get()
    if key:
        _progress.pop(key, None)
    _current.reset(token)


def update(stage=None, model=None, *, activity=False):
    state = _progress.get(_current.get())
    if state is None:
        return
    now = datetime.now(timezone.utc)
    if stage is not None:
        state.update(stage=stage, stage_started_at=now, model=model, generated_chunks=0)
    if activity:
        state["last_activity_at"] = now
        state["generated_chunks"] += 1


def detach():
    _current.set(None)


def snapshot(run_id):
    return dict(_progress.get(str(run_id), {}))
