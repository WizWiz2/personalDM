from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from app.paths import legacy_data_dir, saves_dir


MIGRATION_MARKER = ".legacy-project-data-imported"


def _copy_missing_tree(source: Path, destination: Path) -> int:
    copied = 0
    for item in source.rglob("*"):
        relative = item.relative_to(source)
        target = destination / relative
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if not item.is_file() or target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
        copied += 1
    return copied


def migrate_legacy_project_data(
    *,
    legacy: Path | None = None,
    target: Path | None = None,
) -> dict[str, object]:
    """Copy old repo-local saves into the durable user save directory once.

    The legacy directory is intentionally left untouched. That makes the migration rollback-safe:
    a failed/new build can never destroy the only copy of a campaign. Once the new location has
    been used successfully, uninstalling the application naturally removes the obsolete repo copy.
    """
    source = (legacy or legacy_data_dir()).resolve()
    destination = (target or saves_dir()).resolve()
    marker = destination / MIGRATION_MARKER

    if source == destination:
        return {"status": "same_location", "source": str(source), "target": str(destination)}
    if not source.exists():
        return {"status": "nothing_to_migrate", "source": str(source), "target": str(destination)}

    source_db = source / "campaign.db"
    target_db = destination / "campaign.db"
    if target_db.exists():
        return {
            "status": "target_already_active",
            "source": str(source),
            "target": str(destination),
        }

    destination.mkdir(parents=True, exist_ok=True)
    copied = _copy_missing_tree(source, destination)
    if source_db.is_file() and not target_db.is_file():
        raise RuntimeError(
            f"Legacy campaign database was not copied: {source_db} -> {target_db}"
        )

    marker.write_text(
        f"source={source}\nfiles_copied={copied}\n",
        encoding="utf-8",
    )
    return {
        "status": "migrated",
        "source": str(source),
        "target": str(destination),
        "files_copied": copied,
    }


def _print_result(result: dict[str, object]) -> None:
    status = result["status"]
    if status == "migrated":
        print("[Storage] Existing campaigns copied to durable user storage.")
        print(f"[Storage] From: {result['source']}")
        print(f"[Storage] To:   {result['target']}")
        print("[Storage] The old copy is preserved for safety until the project is removed.")
    elif status == "target_already_active":
        print(f"[Storage] Saves: {result['target']}")
    elif status == "nothing_to_migrate":
        print(f"[Storage] Saves will be stored in: {result['target']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="PersonalDM save-storage migration")
    parser.add_argument("--migrate", action="store_true", help="import legacy repo-local data")
    args = parser.parse_args()
    if not args.migrate:
        parser.print_help()
        return 0
    try:
        result = migrate_legacy_project_data()
    except (OSError, RuntimeError) as exc:
        print(f"[ERROR] Save migration failed: {exc}")
        return 1
    _print_result(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
