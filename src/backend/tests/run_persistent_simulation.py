"""Persistent entrypoint for the objective-driven autonomous campaign.

Unlike the one-shot benchmark entrypoint, rerunning this file should continue a complete
checkpoint unless the caller explicitly requests PDM_SIM_RESET=1.
"""

try:
    from .run_realistic_simulation import run_realistic_simulation
    from .simulation_checkpoint_resume import configure_persistent_reset_mode
except ImportError:
    from run_realistic_simulation import run_realistic_simulation
    from simulation_checkpoint_resume import configure_persistent_reset_mode


if __name__ == "__main__":
    import asyncio
    import os

    configure_persistent_reset_mode()
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run_realistic_simulation())
