from pathlib import Path

from app.runtime_paths import backend_dir, env_file, is_frozen


def test_development_runtime_uses_backend_env_as_single_source() -> None:
    if is_frozen():
        return

    expected = Path(__file__).resolve().parents[1]
    assert backend_dir() == expected
    assert env_file() == expected / ".env"
