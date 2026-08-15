from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Iterable

try:
    from .simulation_dynamic_campaign import CampaignCatalog
except ImportError:
    from simulation_dynamic_campaign import CampaignCatalog


FIXTURE_VERSION = "reliable-soak-world-v1"
FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "reliable_soak_world_v1.json"
_PROVENANCE: dict[str, Any] = {}


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def corpus_sha256(scripted_turns: Iterable[object]) -> str:
    payload = json.dumps(
        list(scripted_turns),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256_bytes(payload)


def fixture_sha256(path: Path = FIXTURE_PATH) -> str:
    return _sha256_bytes(path.read_bytes())


def _validate_fixture_references(catalog: CampaignCatalog) -> None:
    roster = {
        npc.name.casefold(): npc.name
        for arc in catalog.arcs
        for npc in arc.npcs
    }
    if not roster:
        raise ValueError("Reliable soak fixture has no NPC roster")

    introduced: set[str] = set()
    phase_slugs: set[str] = set()
    phase_titles: set[str] = set()
    for arc in catalog.arcs:
        for phase in arc.phases:
            if phase.slug in phase_slugs:
                raise ValueError(f"Reliable soak fixture reuses phase slug {phase.slug!r}")
            phase_slugs.add(phase.slug)
            title_key = phase.title.casefold()
            if title_key in phase_titles:
                raise ValueError(f"Reliable soak fixture reuses phase title {phase.title!r}")
            phase_titles.add(title_key)

            for name in phase.introduced_npcs:
                folded = name.casefold()
                if folded not in roster:
                    raise ValueError(
                        f"Reliable soak fixture introduces unknown NPC {name!r} in {phase.slug}"
                    )
                introduced.add(folded)

            active = {name.casefold() for name in phase.active_npcs}
            unknown = sorted(name for name in active if name not in roster)
            if unknown:
                raise ValueError(
                    f"Reliable soak fixture activates unknown NPCs {unknown} in {phase.slug}"
                )
            premature = sorted(name for name in active if name not in introduced)
            if premature:
                raise ValueError(
                    f"Reliable soak fixture activates NPCs before introduction {premature} "
                    f"in {phase.slug}"
                )

            for thesis in phase.opening_theses:
                inactive = [
                    name
                    for name in thesis.related_names
                    if name.casefold() not in active
                ]
                if inactive:
                    raise ValueError(
                        f"Reliable soak fixture thesis references inactive NPCs {inactive} "
                        f"in {phase.slug}"
                    )
            for pulse in phase.pulses:
                inactive = [
                    name
                    for name in pulse.thesis.related_names
                    if name.casefold() not in active
                ]
                if inactive:
                    raise ValueError(
                        f"Reliable soak fixture pulse references inactive NPCs {inactive} "
                        f"in {phase.slug}"
                    )


def load_reliable_fixture(path: Path = FIXTURE_PATH) -> CampaignCatalog:
    catalog = CampaignCatalog.model_validate_json(path.read_text(encoding="utf-8"))
    _validate_fixture_references(catalog)
    if len(catalog.runtime_phases()) < 8:
        raise ValueError("Reliable 30-turn fixture must contain at least eight phases")
    return catalog


async def fixed_ensure_phase_available(
    *,
    path: Path,
    reset: bool,
    phase_index: int,
    provider,
    router,
    selection,
    previous_outcomes: list[str],
) -> CampaignCatalog:
    del provider, router, selection, previous_outcomes
    catalog = load_reliable_fixture()
    phases = catalog.runtime_phases()
    if phase_index >= len(phases):
        raise RuntimeError(
            f"Reliable soak fixture exhausted at phase {phase_index}; "
            f"fixture contains {len(phases)} phases"
        )

    source = FIXTURE_PATH.read_text(encoding="utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    if reset or not path.exists():
        path.write_text(source, encoding="utf-8")
    elif path.read_text(encoding="utf-8") != source:
        raise RuntimeError(
            "Reliable soak scenario artifact diverged from committed fixture; "
            "start a fresh run instead of mixing setup worlds"
        )
    return catalog


async def deterministic_fixture_character_card(
    provider,
    config,
    api_key,
    seed,
    location_id,
):
    del provider, config, api_key
    try:
        from . import run_realistic_simulation_v2 as runtime
    except ImportError:
        import run_realistic_simulation_v2 as runtime
    return runtime.deterministic_fallback_card(seed, location_id), "fixture"


def _git_output(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def collect_provenance(
    scripted_turns: Iterable[object],
    *,
    repo_root: Path | None = None,
    allow_dirty: bool | None = None,
) -> dict[str, Any]:
    if repo_root is None:
        root_text = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
        repo_root = Path(root_text)
    root = repo_root.resolve()
    commit = _git_output(root, "rev-parse", "HEAD")
    status = _git_output(root, "status", "--porcelain", "--untracked-files=all")
    dirty = bool(status)
    if allow_dirty is None:
        allow_dirty = os.getenv("PDM_SIM_ALLOW_DIRTY", "0") == "1"
    if dirty and not allow_dirty:
        preview = " | ".join(status.splitlines()[:8])
        raise RuntimeError(
            "Reliable soak requires a clean git working tree for comparable A/B results. "
            f"Dirty entries: {preview}. Commit/stash them or explicitly set "
            "PDM_SIM_ALLOW_DIRTY=1 to run a documented non-comparable diagnostic."
        )
    return {
        "commit": commit,
        "dirty": dirty,
        "dirty_status_sha256": _sha256_bytes(status.encode("utf-8")) if dirty else None,
        "fixture_version": FIXTURE_VERSION,
        "fixture_sha256": fixture_sha256(),
        "corpus_sha256": corpus_sha256(scripted_turns),
        "model": os.getenv("PDM_SIM_MODEL", "gemma4:e4b"),
        "base_url": os.getenv("PDM_SIM_BASE_URL", "http://127.0.0.1:11434/v1"),
    }


def provenance_snapshot() -> dict[str, Any]:
    return dict(_PROVENANCE)


def install_reliable_fixture(facade, scripted_turns: Iterable[object]) -> dict[str, Any]:
    global _PROVENANCE
    _PROVENANCE = collect_provenance(scripted_turns)
    facade.runtime.ensure_phase_available = fixed_ensure_phase_available
    facade._original_build_character_card = deterministic_fixture_character_card
    return provenance_snapshot()


__all__ = [
    "FIXTURE_PATH",
    "FIXTURE_VERSION",
    "collect_provenance",
    "corpus_sha256",
    "deterministic_fixture_character_card",
    "fixed_ensure_phase_available",
    "fixture_sha256",
    "install_reliable_fixture",
    "load_reliable_fixture",
    "provenance_snapshot",
]
