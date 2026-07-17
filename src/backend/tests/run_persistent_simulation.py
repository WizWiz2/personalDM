"""Compatibility entrypoint for the realistic autonomous campaign benchmark."""

try:
    from .run_realistic_simulation import run_realistic_simulation
except ImportError:
    from run_realistic_simulation import run_realistic_simulation


if __name__ == "__main__":
    import asyncio
    import os

    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run_realistic_simulation())
