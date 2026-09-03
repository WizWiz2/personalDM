from __future__ import annotations

from live_model_contracts.snapshot import TruthSnapshot
from live_model_contracts.world import FixtureWorld


def undo_item_drop_oracle(
    before: TruthSnapshot,
    after: TruthSnapshot,
    world: FixtureWorld,
) -> list[str]:
    """Prove both halves of undo: a deterministic drop happened, then ownership returned."""
    failures: list[str] = []
    key_id = world.items.get("латунный ключ")
    key = next(
        (
            row
            for row in after.entity_rows(entity_type="item")
            if row.get("id") == key_id
        ),
        None,
    )
    if key is None:
        failures.append("fixture key disappeared after undo")
    else:
        if key.get("owner") != "Кай":
            failures.append(f"undo did not restore key owner: {key}")
        if key.get("location") is not None:
            failures.append(f"undo left key in world location: {key}")

    undone_drop_steps = [
        step
        for sequence in after.data.get("action_sequences", [])
        if sequence.get("status") == "undone"
        for step in sequence.get("steps", [])
        if step.get("status") == "undone"
        and step.get("action_type") == "inventory"
        and step.get("item_operation") == "drop"
    ]
    if not undone_drop_steps:
        failures.append(
            "undo did not prove an applied deterministic inventory drop before compensation"
        )
    return failures


__all__ = ["undo_item_drop_oracle"]
