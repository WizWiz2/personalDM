from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any


def memory_evidence(
    assistant_content: str,
    receipts: Iterable[dict[str, Any]] | None,
) -> str:
    """Published prose plus the typed fields of executor receipts.

    A receipt's free-text outcome is the plan's wording, which the narrator may not have
    published (a step can say the hero set off when the prose never had him leave), so memory
    sees only typed fields next to the prose that was actually shown.
    """
    parts = [str(assistant_content or "").strip()]
    rendered: list[str] = []
    for index, receipt in enumerate(receipts or (), start=1):
        payload = receipt.get("payload") if isinstance(receipt, dict) else None
        compact = {
            key: payload.get(key)
            for key in (
                "status", "action_type", "resolution", "item_operation", "item_id", "item_name",
                "from_character_id", "to_character_id", "destination_location",
            )
            if isinstance(payload, dict) and payload.get(key) is not None
        }
        if compact:
            rendered.append(f"R{index}: " + json.dumps(compact, ensure_ascii=False, sort_keys=True,
                                                     default=str))
    if rendered:
        parts.append("[EXECUTED WORLD RESULTS]\n" + "\n".join(rendered))
    return "\n\n".join(part for part in parts if part)


__all__ = ["memory_evidence"]
