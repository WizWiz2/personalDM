from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any


def memory_evidence(
    assistant_content: str,
    receipts: Iterable[dict[str, Any]] | None,
) -> str:
    """Build immutable memory evidence from published prose plus executor receipts.

    Receipt descriptions are authoritative observable outcomes produced by structured execution.
    The compact payload is included only as machine context; models are still required to quote an
    exact evidence span from this assembled text when proposing durable semantic state.
    """
    parts = [str(assistant_content or "").strip()]
    rendered: list[str] = []
    for index, receipt in enumerate(receipts or (), start=1):
        if not isinstance(receipt, dict):
            continue
        description = str(receipt.get("description") or "").strip()
        payload = receipt.get("payload") if isinstance(receipt.get("payload"), dict) else {}
        if not description and not payload:
            continue
        line = f"R{index}: {description}" if description else f"R{index}:"
        if payload:
            compact = {
                key: payload.get(key)
                for key in (
                    "status",
                    "action_type",
                    "resolution",
                    "item_operation",
                    "item_id",
                    "item_name",
                    "from_character_id",
                    "to_character_id",
                    "destination_location",
                    "observable_outcome",
                )
                if payload.get(key) is not None
            }
            if compact:
                line += "\n" + json.dumps(compact, ensure_ascii=False, sort_keys=True, default=str)
        rendered.append(line)
    if rendered:
        parts.append("[EXECUTED WORLD RESULTS]\n" + "\n".join(rendered))
    return "\n\n".join(part for part in parts if part)


__all__ = ["memory_evidence"]
