"""Deterministic 30-turn quality soak with a versioned world fixture.

Unlike the autonomous benchmark, this entrypoint removes both stochastic setup and the
LLM player from the measured path. Planner/Narrator/Validator/Scribe/Curator/Evaluator
remain real model-backed roles. The committed world fixture and scripted player corpus
make A/B comparisons reproducible across engine commits.
"""

from __future__ import annotations

import asyncio
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


def configure_reliable_soak() -> None:
    os.environ.setdefault("PDM_SIM_MODE", "quality")
    os.environ.setdefault("PDM_SIM_TURNS", "30")
    os.environ.setdefault("PDM_SIM_RESET", "1")
    os.environ.setdefault("PDM_SIM_PLAYER_SOURCE", "scripted")

    # Fail before migrations or LLM calls if the local tree cannot support a clean A/B.
    # The fixture hook also replaces dynamic scenario generation and the stochastic NPC
    # Character Builder used only to seed benchmark setup.
    install_reliable_fixture(facade, SCRIPTED_TURNS)
    install_phase_location_reuse(facade.runtime)
    install_reliable_soak(facade)


async def run() -> None:
    configure_reliable_soak()
    await facade.run_realistic_simulation()


if __name__ == "__main__":
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run())
