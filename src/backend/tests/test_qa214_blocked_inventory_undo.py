"""A blocked inventory step must never mutate items when undo compensates a sequence."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.services.action_sequence_executor import ActionSequenceExecutor


@pytest.mark.asyncio
async def test_compensation_ignores_item_receipts_for_blocked_and_skipped_steps():
    # Blocked/skipped steps can carry item IDs and uninitialized previous-state
    # fields, but those fields must never be applied to the persistent item.
    session = MagicMock()
    session.execute = AsyncMock()
    session.get = AsyncMock()
    session.flush = AsyncMock()
    steps = [
        SimpleNamespace(
            status=status, item_id=str(uuid4()),
            transition_id=None,
            item_previous_owner_id=None,
            item_previous_location_id=None,
            item_previous_placement_detail=None,
        )
        for status in ("blocked", "skipped")
    ]
    session.execute.return_value.scalars.return_value.all.return_value = steps
    sequence = SimpleNamespace(id=str(uuid4()), status="applied", undone_at=None)
    executor = ActionSequenceExecutor(session)
    await executor._compensate(sequence, "undone")

    assert [step.status for step in steps] == ["undone", "undone"]
    session.get.assert_not_awaited()
    assert sequence.status == "undone"
