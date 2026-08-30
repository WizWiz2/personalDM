from __future__ import annotations

import os
import sys
from pathlib import Path


PRODUCT_DIR_NAME = "PersonalDM"
BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parents[1]
BACKEND_ENV_FILE = BACKEND_DIR / ".env"


def app_home() -> Path:
    """Return the per-user PersonalDM home without creating it.

    PDM_HOME is an explicit escape hatch for portable/test installations. Normal Windows
    installs live under LocalAppData because saves and generated art can be large and should
    not roam with a user profile.
    """
    override = os.environ.get("PDM_HOME", "").strip()
    if override:
        return Path(override).expanduser().resolve()

    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / PRODUCT_DIR_NAME
        return Path.home() / "AppData" / "Local" / PRODUCT_DIR_NAME

    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / PRODUCT_DIR_NAME

    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return base / PRODUCT_DIR_NAME


def saves_dir() -> Path:
    return app_home() / "saves"


def logs_dir() -> Path:
    return app_home() / "logs"


def legacy_data_dir() -> Path:
    return BACKEND_DIR / "data"


def sqlite_url(path: Path) -> str:
    """Build an absolute SQLAlchemy aiosqlite URL for a filesystem path."""
    absolute = path.expanduser().resolve().as_posix()
    if os.name == "nt":
        return f"sqlite+aiosqlite:///{absolute}"
    return f"sqlite+aiosqlite:////{absolute.lstrip('/')}"


__all__ = [
    "APP_HOME",
    "BACKEND_DIR",
    "BACKEND_ENV_FILE",
    "REPO_ROOT",
    "app_home",
    "legacy_data_dir",
    "logs_dir",
    "saves_dir",
    "sqlite_url",
]

# Convenient immutable snapshot for callers that do not need to react to PDM_HOME changes.
APP_HOME = app_home()
