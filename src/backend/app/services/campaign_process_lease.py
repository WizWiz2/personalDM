"""OS-owned campaign lease for local SQLite workers; process death releases it.

The lease covers admission, every saga commit and recovery. It does not hold a
SQLite writer transaction across model calls. All engines addressing the same
resolved database path use the same lock; unrelated campaigns remain independent.
"""
import os
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path


class GenerationAlreadyRunningError(ValueError):
    pass


_owned: ContextVar[dict | None] = ContextVar("campaign_process_leases", default=None)


def _path(bind, campaign_id):
    database = bind.url.database
    if database and database != ":memory:":
        root = Path(database).resolve()
        directory = root.parent / (root.name + ".turn-locks")
    else:
        directory = Path(tempfile.gettempdir()) / f"pdm-memory-{os.getpid()}-{id(bind)}"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{campaign_id}.lock"


class _Lease:
    def __init__(self, path):
        self.file = path.open("a+b")
        self.transferred = False
        try:
            if os.name == "nt":
                import msvcrt

                if self.file.seek(0, 2) == 0:
                    self.file.write(b"\0")
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise GenerationAlreadyRunningError("Previous player turn is still being resolved") from exc

    def close(self, _task=None):
        self.file.close()  # closing the handle releases the OS lock, also after a crash

    def transfer_to(self, task):
        self.transferred = True
        task.add_done_callback(self.close)


@contextmanager
def campaign_process_lease(bind, campaign_id):
    path = _path(bind, campaign_id)
    held = (_owned.get() or {}).get(path)
    if held is not None and not held.file.closed:
        yield held
        return
    lease = _Lease(path)
    token = _owned.set({**(_owned.get() or {}), path: lease})
    try:
        yield lease
    finally:
        _owned.reset(token)
        if not lease.transferred:
            lease.close()
