"""Resolve install vs bundle paths for dev and frozen (PyInstaller) builds."""
from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"))


@lru_cache(maxsize=1)
def install_dir() -> Path:
    """Writable directory that contains PersonalDM.exe (or repo root in dev)."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    # src/backend/app/runtime_paths.py -> parents[3] = repo root
    return Path(__file__).resolve().parents[3]


@lru_cache(maxsize=1)
def bundle_dir() -> Path:
    """Read-only packaged resources (PyInstaller extract) or backend dir in dev."""
    if is_frozen():
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parents[2]


@lru_cache(maxsize=1)
def backend_dir() -> Path:
    return bundle_dir() if is_frozen() else Path(__file__).resolve().parents[2]


@lru_cache(maxsize=1)
def frontend_dir() -> Path:
    if is_frozen():
        return bundle_dir() / "frontend"
    return install_dir() / "src" / "frontend"


@lru_cache(maxsize=1)
def frontend_dist_dir() -> Path:
    return frontend_dir() / "dist"


@lru_cache(maxsize=1)
def tools_dir() -> Path:
    return install_dir() / "tools"


@lru_cache(maxsize=1)
def env_file() -> Path:
    # Keep player config next to the exe (or backend/.env in dev).
    if is_frozen():
        return install_dir() / ".env"
    return backend_dir() / ".env"


@lru_cache(maxsize=1)
def is_packaged_dist() -> bool:
    """True for PyInstaller builds or zip DIST_MODE marker."""
    if is_frozen():
        return True
    return (install_dir() / "DIST_MODE").is_file()
