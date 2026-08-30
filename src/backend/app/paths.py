from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Mapping

APP_DIR_NAME = "PersonalDM"


def user_root(
    *,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
    home: Path | None = None,
) -> Path:
    """Return the per-user PersonalDM root using the native desktop convention."""
    env = os.environ if environ is None else environ
    override = str(env.get("PDM_USER_ROOT") or "").strip()
    if override:
        return Path(override).expanduser().resolve()

    current_platform = platform or sys.platform
    user_home = (home or Path.home()).expanduser()
    if current_platform.startswith("win"):
        base = env.get("LOCALAPPDATA") or env.get("APPDATA")
        if base:
            return Path(base).expanduser() / APP_DIR_NAME
        return user_home / "AppData" / "Local" / APP_DIR_NAME
    if current_platform == "darwin":
        return user_home / "Library" / "Application Support" / APP_DIR_NAME

    xdg = str(env.get("XDG_DATA_HOME") or "").strip()
    base = Path(xdg).expanduser() if xdg else user_home / ".local" / "share"
    return base / APP_DIR_NAME


def games_dir() -> Path:
    return user_root() / "games"


def runtime_dir() -> Path:
    return user_root() / "runtime"


def logs_dir() -> Path:
    return user_root() / "logs"


def install_dir() -> Path:
    return user_root() / "install"


def default_data_dir() -> str:
    return str(games_dir())


def default_database_url() -> str:
    path = (games_dir() / "campaign.db").absolute().as_posix()
    return f"sqlite+aiosqlite:///{path}"


def _copy_missing_tree(source: Path, destination: Path) -> list[str]:
    copied: list[str] = []
    if not source.is_dir():
        return copied
    for item in source.rglob("*"):
        if not item.is_file():
            continue
        relative = item.relative_to(source)
        target = destination / relative
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".migration.tmp")
        shutil.copy2(item, temporary)
        temporary.replace(target)
        copied.append(relative.as_posix())
    return copied


def migrate_legacy_game_data(root_dir: Path, backend_dir: Path) -> dict:
    """Copy legacy checkout-local saves into the per-user games directory once.

    The legacy source is deliberately left untouched. This gives users a reversible
    migration and lets the uninstaller explain exactly which copy it is deleting.
    Existing destination files always win; migration never overwrites a newer save.
    """
    destination = games_dir()
    destination.mkdir(parents=True, exist_ok=True)
    legacy_candidates = [backend_dir / "data", root_dir / "data"]
    copied: list[str] = []
    sources: list[str] = []
    for source in legacy_candidates:
        try:
            if source.resolve() == destination.resolve():
                continue
        except OSError:
            pass
        if not source.is_dir():
            continue
        sources.append(str(source))
        copied.extend(_copy_missing_tree(source, destination))

    return {
        "destination": str(destination),
        "sources": sources,
        "copied": copied,
        "database": str(destination / "campaign.db"),
    }


def ensure_user_layout() -> dict[str, str]:
    paths = {
        "root": user_root(),
        "games": games_dir(),
        "runtime": runtime_dir(),
        "logs": logs_dir(),
        "install": install_dir(),
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return {name: str(path) for name, path in paths.items()}


__all__ = [
    "APP_DIR_NAME",
    "default_data_dir",
    "default_database_url",
    "ensure_user_layout",
    "games_dir",
    "install_dir",
    "logs_dir",
    "migrate_legacy_game_data",
    "runtime_dir",
    "user_root",
]
