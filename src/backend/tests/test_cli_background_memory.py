import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

import cli
from app.services.post_turn_dispatcher import PostTurnDispatcher


@pytest.mark.asyncio
async def test_cli_returns_to_input_without_waiting_for_memory(monkeypatch):
    async def stream():
        yield "Проход закрыт."

    view = SimpleNamespace(
        campaign_name="CLI", npcs=[], scene=SimpleNamespace(title="Причал", mood=None)
    )
    application = SimpleNamespace(
        current_scene_view=AsyncMock(return_value=view),
        route_input=AsyncMock(return_value=SimpleNamespace(channel="narrative", stream=stream())),
        latest_assistant_turn_id=AsyncMock(return_value=uuid4()),
        post_turn_status=AsyncMock(return_value=SimpleNamespace(failed_count=0)),
    )
    monkeypatch.setattr(cli, "GameApplication", lambda session: application)
    monkeypatch.setattr(
        cli,
        "SessionZeroService",
        lambda session: SimpleNamespace(
            get=AsyncMock(return_value=SimpleNamespace(status="completed"))
        ),
    )
    monkeypatch.setattr(cli, "recover_stale_post_turn_jobs", AsyncMock())
    monkeypatch.setattr(cli, "clear_screen", lambda: None)
    inputs = iter(["Осматриваю проход.", "/exit"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(inputs))
    memory = asyncio.create_task(asyncio.Event().wait(), name="slow-memory")
    PostTurnDispatcher._tasks.add(memory)
    try:
        await asyncio.wait_for(cli.play_game_loop(uuid4(), None), timeout=2)
        assert not memory.done()
        application.route_input.assert_awaited_once()
    finally:
        PostTurnDispatcher._tasks.discard(memory)
        memory.cancel()
        await asyncio.gather(memory, return_exceptions=True)
