"""Compatibility entrypoint for the objective-driven autonomous campaign v4."""

try:
    from .run_realistic_simulation_v4 import run_realistic_simulation_v4
except ImportError:
    from run_realistic_simulation_v4 import run_realistic_simulation_v4


if __name__ == "__main__":
    import asyncio
    import os

    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run_realistic_simulation_v4())
