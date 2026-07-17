from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

try:
    from . import run_realistic_simulation_v2 as runtime
    from . import run_realistic_simulation_v3 as v3
except ImportError:
    import run_realistic_simulation_v2 as runtime
    import run_realistic_simulation_v3 as v3

_original_generate_player_decision = runtime.generate_player_decision


def _retry_decision_from_trace() -> runtime.PlayerDecision | None:
    data_dir = Path(os.getenv("PDM_SIM_DATA_DIR", "./data"))
    state_path = data_dir / "realistic_simulation_state.json"
    trace_path = data_dir / "realistic_simulation_trace.jsonl"
    if not state_path.exists() or not trace_path.exists():
        return None
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        logical_turn = int(state["logical_turn"])
        trace = runtime.TraceStore(trace_path)
        record = trace.records.get(logical_turn)
        if not record or not record.get("generation_failed"):
            return None
        player = record.get("player") or {}
        return runtime.PlayerDecision(
            target=str(player["target"]),
            mode=str(player["mode"]),
            intent=str(player["intent"]),
        )
    except Exception:
        return None


async def resumable_generate_player_decision(*args, **kwargs):
    previous = _retry_decision_from_trace()
    if previous is not None:
        return previous
    return await _original_generate_player_decision(*args, **kwargs)


async def run_realistic_simulation_v4() -> None:
    runtime.generate_player_decision = resumable_generate_player_decision
    await v3.run_realistic_simulation_v3()


if __name__ == "__main__":
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run_realistic_simulation_v4())
