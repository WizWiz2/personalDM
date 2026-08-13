from pathlib import Path

from tests.simulation_checkpoint_resume import (
    CHECKPOINT_FILES,
    checkpoint_is_resumable,
    configure_persistent_reset_mode,
)


def _complete_checkpoint(root: Path, database: Path | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    db = database or root / "realistic_simulation.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_bytes(b"sqlite checkpoint")
    for name in CHECKPOINT_FILES:
        (root / name).write_text("{}\n", encoding="utf-8")
    return db


def test_complete_successful_checkpoint_resumes_by_default(tmp_path, monkeypatch):
    _complete_checkpoint(tmp_path)
    monkeypatch.delenv("PDM_SIM_RESET", raising=False)
    monkeypatch.setenv("PDM_SIM_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("PDM_SIM_DB", raising=False)

    assert checkpoint_is_resumable(tmp_path) is True
    assert configure_persistent_reset_mode() == "0"
    assert __import__("os").environ["PDM_SIM_RESET"] == "0"


def test_incomplete_checkpoint_fails_closed_to_fresh_start(tmp_path, monkeypatch):
    _complete_checkpoint(tmp_path)
    (tmp_path / "realistic_simulation_trace.jsonl").unlink()
    monkeypatch.delenv("PDM_SIM_RESET", raising=False)
    monkeypatch.setenv("PDM_SIM_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("PDM_SIM_DB", raising=False)

    assert checkpoint_is_resumable(tmp_path) is False
    assert configure_persistent_reset_mode() == "1"


def test_explicit_reset_setting_always_wins(tmp_path, monkeypatch):
    _complete_checkpoint(tmp_path)
    monkeypatch.setenv("PDM_SIM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PDM_SIM_RESET", "1")

    assert configure_persistent_reset_mode() == "1"

    monkeypatch.setenv("PDM_SIM_RESET", "0")
    assert configure_persistent_reset_mode() == "0"


def test_custom_database_path_can_resume_complete_checkpoint(tmp_path, monkeypatch):
    data_dir = tmp_path / "state"
    database = tmp_path / "db" / "soak.sqlite"
    _complete_checkpoint(data_dir, database)
    monkeypatch.delenv("PDM_SIM_RESET", raising=False)
    monkeypatch.setenv("PDM_SIM_DATA_DIR", str(data_dir))
    monkeypatch.setenv("PDM_SIM_DB", str(database))

    assert checkpoint_is_resumable(data_dir, database) is True
    assert configure_persistent_reset_mode() == "0"
