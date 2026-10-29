
from app.runtime_paths import backend_dir, env_file, is_frozen


def test_development_runtime_uses_backend_env_as_single_source() -> None:
    if is_frozen():
        return

    root = backend_dir()
    assert (root / "pyproject.toml").is_file()
    assert (root / "app" / "runtime_paths.py").is_file()
    assert env_file() == root / ".env"
