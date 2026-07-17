"""Compatibility entrypoint for the objective-driven autonomous campaign v3."""

try:
    from .run_realistic_simulation_v3 import run_realistic_simulation_v3
except ImportError:
    from run_realistic_simulation_v3 import run_realistic_simulation_v3


if __name__ == "__main__":
    import asyncio
    import os

    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run_realistic_simulation_v3())
