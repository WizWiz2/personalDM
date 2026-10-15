from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.models.proposed_change import ChangeType
from app.services.post_turn_processor import PostTurnProcessor


@pytest.mark.asyncio
async def test_auto_commit_applies_only_valid_safe_proposals(monkeypatch):
    session = AsyncMock()
    processor = PostTurnProcessor(session)
    campaign_id = uuid4()
    turn_id = uuid4()

    fact = SimpleNamespace(
        id=uuid4(),
        status="proposed",
        change_type=ChangeType.FACT.value,
        payload={"subject": "Кассиан", "predicate": "статус", "object_value": "пропал"},
    )
    invalid = SimpleNamespace(
        id=uuid4(),
        status="invalid",
        change_type=ChangeType.EVENT.value,
        payload={"_validation_error": "bad reference"},
    )
    gap = SimpleNamespace(
        id=uuid4(),
        status="proposed",
        change_type=ChangeType.CANON_GAP.value,
        payload={"description": "missing entity"},
    )

    applier = SimpleNamespace(apply=AsyncMock())
    repo = SimpleNamespace(resolve=AsyncMock())
    monkeypatch.setattr(
        "app.services.post_turn_processor.CanonApplier",
        lambda _session: applier,
    )
    monkeypatch.setattr(
        "app.services.post_turn_processor.ProposedChangeRepository",
        lambda _session: repo,
    )

    applied, staged = await processor._auto_commit_proposals(
        campaign_id,
        turn_id,
        [fact, invalid, gap],
    )

    assert applied == 1
    assert staged == 2
    applier.apply.assert_awaited_once_with(
        campaign_id,
        ChangeType.FACT,
        fact.payload,
        turn_id,
    )
    repo.resolve.assert_awaited_once()
    assert repo.resolve.await_args.args[0] == fact.id
    assert repo.resolve.await_args.args[1].status == "accepted"


@pytest.mark.asyncio
async def test_auto_commit_includes_transient_narrative_details(monkeypatch):
    session = AsyncMock()
    processor = PostTurnProcessor(session)
    detail = SimpleNamespace(
        id=uuid4(),
        status="proposed",
        change_type=ChangeType.NARRATIVE_DETAIL.value,
        payload={"scene_id": str(uuid4()), "text": "Запах машинного масла"},
    )
    applier = SimpleNamespace(apply=AsyncMock())
    repo = SimpleNamespace(resolve=AsyncMock())
    monkeypatch.setattr(
        "app.services.post_turn_processor.CanonApplier",
        lambda _session: applier,
    )
    monkeypatch.setattr(
        "app.services.post_turn_processor.ProposedChangeRepository",
        lambda _session: repo,
    )

    applied, staged = await processor._auto_commit_proposals(
        uuid4(),
        uuid4(),
        [detail],
    )

    assert (applied, staged) == (1, 0)
    assert applier.apply.await_args.args[1] == ChangeType.NARRATIVE_DETAIL


@pytest.mark.asyncio
async def test_simulation_marker_preserves_external_proposal_resolution():
    session = AsyncMock()
    session.get.return_value = SimpleNamespace(
        context_snapshot='{"simulation":{"run_id":"test-run","logical_turn":4}}'
    )
    processor = PostTurnProcessor(session)

    assert await processor._uses_external_proposal_resolution(uuid4()) is True


@pytest.mark.asyncio
async def test_normal_turn_uses_memory_auto_commit():
    session = AsyncMock()
    session.get.return_value = SimpleNamespace(context_snapshot='{"channel":"narrative"}')
    processor = PostTurnProcessor(session)

    assert await processor._uses_external_proposal_resolution(uuid4()) is False


@pytest.mark.asyncio
async def test_scribe_cannot_auto_transfer_item_from_player_pocket(monkeypatch):
    """F-14: a delayed prose guess must not silently change Item.current_owner_id."""
    session = AsyncMock()
    session.scalar.return_value = str(uuid4())
    processor = PostTurnProcessor(session)
    proposal = SimpleNamespace(
        id=uuid4(),
        status="proposed",
        change_type=ChangeType.ITEM_TRANSFER.value,
        payload={
            "item_id": str(uuid4()),
            "owner_id": str(uuid4()),
            "description": "Scribe guessed the registrar took the bead",
        },
    )
    applier = SimpleNamespace(apply=AsyncMock())
    repo = SimpleNamespace(resolve=AsyncMock())
    monkeypatch.setattr(
        "app.services.post_turn_processor.CanonApplier", lambda _session: applier,
    )
    monkeypatch.setattr(
        "app.services.post_turn_processor.ProposedChangeRepository",
        lambda _session: repo,
    )
    applied, staged = await processor._auto_commit_proposals(
        uuid4(), uuid4(), [proposal],
    )
    assert (applied, staged) == (0, 1)
    applier.apply.assert_not_awaited()
    assert repo.resolve.await_args.args[1].status == "rejected"
