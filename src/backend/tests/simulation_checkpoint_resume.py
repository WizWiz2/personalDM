from __future__ import annotations

import os
from pathlib import Path


CHECKPOINT_FILES = (
    "realistic_simulation_state.json",
    "realistic_simulation_trace.jsonl",
    "realistic_simulation_scenario.json",
)


def checkpoint_is_resumable(
    data_dir: Path,
    database_path: Path | None = None,
) -> bool:
    """Return True only for a complete persisted simulation checkpoint.

    A successful process checkpoint needs both durable game state in SQLite and the
    simulation's state/trace/scenario files. A partial directory fails closed to a fresh
    run rather than mixing old and new benchmark state.
    """
    database = database_path or data_dir / "realistic_simulation.db"
    return database.exists() and all((data_dir / name).exists() for name in CHECKPOINT_FILES)


def configure_persistent_reset_mode() -> str:
    """Choose fresh-start vs resume for the persistent runner.

    Explicit PDM_SIM_RESET always wins. Otherwise the persistent entrypoint resumes a
    complete checkpoint and starts fresh only when no complete checkpoint exists.
    """
    explicit = os.getenv("PDM_SIM_RESET")
    if explicit is not None:
        return explicit

    data_dir = Path(os.getenv("PDM_SIM_DATA_DIR", "./data"))
    database = Path(
        os.getenv(
            "PDM_SIM_DB",
            str(data_dir / "realistic_simulation.db"),
        )
    )
    value = "0" if checkpoint_is_resumable(data_dir, database) else "1"
    os.environ["PDM_SIM_RESET"] = value
    return value


__all__ = ["checkpoint_is_resumable", "configure_persistent_reset_mode"]
