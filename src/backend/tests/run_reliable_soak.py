"""Deterministic 30-turn quality soak with a versioned world fixture.

Unlike the autonomous benchmark, this entrypoint removes both stochastic setup and the
LLM player from the measured path. Planner/Narrator/Validator/Scribe/Curator/Evaluator
remain real model-backed roles. The committed world fixture and scripted player corpus
make A/B comparisons reproducible across engine commits.
"""

from __future__ import annotations

import asyncio
import json
import os

try:
    from . import run_realistic_simulation as facade
    from .simulation_phase_location_reuse import install_phase_location_reuse
    from .simulation_reliable_fixture import install_reliable_fixture
    from .simulation_reliable_soak import SCRIPTED_TURNS, install_reliable_soak
except ImportError:
    import run_realistic_simulation as facade
    from simulation_phase_location_reuse import install_phase_location_reuse
    from simulation_reliable_fixture import install_reliable_fixture
    from simulation_reliable_soak import SCRIPTED_TURNS, install_reliable_soak


def _install_provenance_report(provenance: dict[str, object]) -> None:
    original_report = facade._campaign_report

    def report_with_provenance(database_path, data_dir):
        lines = original_report(database_path, data_dir)
        provenance_path = data_dir / "reliable_soak_provenance.json"
        provenance_path.write_text(
            json.dumps(provenance, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        header = [
            f"- Reliable fixture: `{provenance['fixture_version']}`",
            f"- Fixture SHA256: `{provenance['fixture_sha256']}`",
            f"- Scripted corpus SHA256: `{provenance['corpus_sha256']}`",
            f"- Tested commit: `{provenance['commit']}`",
            f"- Tested model: `{provenance['model']}`",
            f"- Tested base URL: `{provenance['base_url']}`",
            f"- Git working tree dirty: **{bool(provenance['dirty'])}**",
            "- Dirty status SHA256: "
            + (f"`{provenance['dirty_status_sha256']}`" if provenance["dirty_status_sha256"] else "none"),
            f"- Provenance artifact: `{provenance_path}`",
        ]
        return [*header, *lines]

    facade._campaign_report = report_with_provenance


def configure_reliable_soak() -> None:
    os.environ.setdefault("PDM_SIM_MODE", "quality")
    os.environ.setdefault("PDM_SIM_TURNS", "30")
    os.environ.setdefault("PDM_SIM_RESET", "1")
    os.environ.setdefault("PDM_SIM_PLAYER_SOURCE", "scripted")

    # Fail before migrations or LLM calls if the local tree cannot support a clean A/B.
    # The fixture hook also replaces dynamic scenario generation and the stochastic NPC
    # Character Builder used only to seed benchmark setup.
    provenance = install_reliable_fixture(facade, SCRIPTED_TURNS)
    install_phase_location_reuse(facade.runtime)
    install_reliable_soak(facade)
    _install_provenance_report(provenance)


async def run() -> None:
    configure_reliable_soak()
    await facade.run_realistic_simulation()


if __name__ == "__main__":
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run())
