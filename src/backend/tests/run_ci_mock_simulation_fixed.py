"""Wrapper that gives mock NPC equipment unique instance names.

The underlying production collision is intentionally preserved in the report:
Item entities are unique by canonical_name per campaign, while Character Builder
can create the same generic equipment name for several characters.
"""

from __future__ import annotations

import asyncio
import json

from tests import run_ci_mock_simulation as simulation


_original_character_card = simulation._character_card


def unique_character_card(text: str) -> str:
    payload = json.loads(_original_character_card(text))
    name = payload["canonical_name"]
    role = payload["visual_profile"]["role"]
    payload["equipment"] = [
        f"{name}'s {role} kit",
        f"{name}'s travel pack",
    ]
    return json.dumps(payload, ensure_ascii=False)


simulation._character_card = unique_character_card


if __name__ == "__main__":
    asyncio.run(simulation.main())
