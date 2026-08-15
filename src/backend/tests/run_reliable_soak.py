"""Deterministic 30-turn quality soak for gameplay/narration diagnostics.

This runner deliberately removes the LLM player-controller from the critical path. It uses a
fixed 30-turn corpus, keeps the real Planner/Narrator/Validator/Scribe stack, and writes the same
SQLite/trace/report artifacts as the realistic simulation with extra publication diagnostics.
"""

from __future__ import annotations

import asyncio
import os

try:
    from . import run_realistic_simulation as facade
    from .simulation_reliable_soak import install_reliable_soak
except ImportError:
    import run_realistic_simulation as facade
    from simulation_reliable_soak import install_reliable_soak


def configure_reliable_soak() -> None:
    os.environ.setdefault("PDM_SIM_MODE", "quality")
    os.environ.setdefault("PDM_SIM_TURNS", "30")
    os.environ.setdefault("PDM_SIM_RESET", "1")
    os.environ.setdefault("PDM_SIM_PLAYER_SOURCE", "scripted")
    install_reliable_soak(facade)


if __name__ == "__main__":
    configure_reliable_soak()
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(facade.run_realistic_simulation())
