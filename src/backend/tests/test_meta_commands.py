from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.turn_repo import TurnRepository
from app.models.campaign import CampaignCreate
from app.models.turn import TurnCreate
from app.services.context_compiler import ContextCompiler
from app.services.meta_command_router import parse_meta_command


def test_meta_parser_requires_a_leading_explicit_command():
    parsed = parse_meta_command("  /dm Почему трактирщик в комнате?")
    assert parsed is not None
    assert parsed.name == "DM"
    assert parsed.query == "Почему трактирщик в комнате?"

    ooc = parse_meta_command("/OOC   проверим состояние сцены")
    assert ooc is not None
    assert ooc.name == "OOC"
    assert ooc.query == "проверим состояние сцены"

    assert parse_meta_command("Я говорю /DM вслух") is None
    assert parse_meta_command("/DMTELL это не команда") is None


def test_dm_command_is_read_only_and_allowed_before_session_zero(client: TestClient):
    campaign_id = client.post(
        "/api/campaigns",
        json={"name": "Meta routing"},
    ).json()["id"]
    scene = client.post(
        f"/api/campaigns/{campaign_id}/scenes",
        json={"title": "Комната над трактиром", "mood": "спокойствие"},
    ).json()

    before = client.get(f"/api/campaigns/{campaign_id}/debugger").json()
    captured = {}

    async def meta_stream(messages, *args, **kwargs):
        captured["messages"] = messages
        yield "Это ошибка пространственной непрерывности: трактирщик не был перемещён в комнату."

    with patch(
        "app.services.meta_command_router.LLMProvider.generate_stream",
        side_effect=meta_stream,
    ), patch(
        "app.services.turn_planner.TurnPlanner.plan",
        new_callable=AsyncMock,
    ) as planner, patch(
        "app.services.post_turn_processor.PostTurnProcessor.enqueue",
        new_callable=AsyncMock,
    ) as enqueue:
        response = client.post(
            f"/api/campaigns/{campaign_id}/turns",
            json={
                "role": "user",
                "content": "/DM Почему трактирщик оказался в моей комнате?",
                "scene_id": scene["id"],
            },
        )

    assert response.status_code == 200
    assert response.headers["x-personaldm-channel"] == "meta"
    assert "ошибка пространственной непрерывности" in response.text
    planner.assert_not_awaited()
    enqueue.assert_not_awaited()

    system = captured["messages"][0].content
    assert "read-only" in system
    assert "не перемещай персонажей" in system
    assert "Комната над трактиром" in system

    after = client.get(f"/api/campaigns/{campaign_id}/debugger").json()
    assert after["campaign"]["current_scene_id"] == before["campaign"]["current_scene_id"]
    assert after["campaign"]["current_scene_id"] == scene["id"]
    assert after["scenes"] == before["scenes"]
    assert after["scene_transitions"] == before["scene_transitions"]
    assert after["post_turn_jobs"] == []
    assert after["generation_runs"] == []
    assert after["proposals"] == []

    history = client.get(
        f"/api/campaigns/{campaign_id}/turns?channel=all"
    ).json()
    assert [turn["role"] for turn in history] == ["meta_user", "meta_assistant"]
    assert all(turn["channel"] == "meta" for turn in history)
    assert all(turn["scene_id"] is None for turn in history)
    assert history[1]["parent_turn_id"] == history[0]["id"]
    assert client.get(
        f"/api/campaigns/{campaign_id}/turns?channel=narrative"
    ).json() == []


def test_ooc_is_an_alias_for_the_same_meta_pipeline(client: TestClient):
    campaign_id = client.post(
        "/api/campaigns",
        json={"name": "OOC alias"},
    ).json()["id"]

    async def meta_stream(*args, **kwargs):
        yield "Сейчас активной сцены нет; сначала заверши нулевую сессию."

    with patch(
        "app.services.meta_command_router.LLMProvider.generate_stream",
        side_effect=meta_stream,
    ):
        response = client.post(
            f"/api/campaigns/{campaign_id}/turns",
            json={"role": "user", "content": "/OOC Где сейчас герой?"},
        )

    assert response.status_code == 200
    assert response.headers["x-personaldm-channel"] == "meta"
    assert "активной сцены нет" in response.text


@pytest.mark.asyncio
async def test_meta_dialogue_never_enters_narrator_context(db_session):
    campaign = await CampaignRepository(db_session).create(
        __import__("uuid").uuid4(),
        CampaignCreate(name="Context isolation"),
    )
    repo = TurnRepository(db_session)
    meta_user = await repo.create(
        campaign.id,
        TurnCreate(
            role="meta_user",
            content="/DM Почему трактирщик здесь?",
        ),
    )
    await repo.create(
        campaign.id,
        TurnCreate(
            role="meta_assistant",
            content="Это ошибка непрерывности.",
            parent_turn_id=meta_user.id,
        ),
    )
    await repo.create(
        campaign.id,
        TurnCreate(role="user", content="Я открываю окно."),
    )
    await db_session.commit()

    messages, metadata = await ContextCompiler(db_session).compile_context(
        campaign_id=campaign.id,
    )
    compiled = "\n".join(message.content for message in messages)
    assert "Я открываю окно" in compiled
    assert "Почему трактирщик здесь" not in compiled
    assert "Это ошибка непрерывности" not in compiled
    assert metadata["history_turns_count"] == 1


@pytest.mark.asyncio
async def test_narrative_undo_ignores_newer_meta_pair(db_session):
    campaign = await CampaignRepository(db_session).create(
        __import__("uuid").uuid4(),
        CampaignCreate(name="Undo isolation"),
    )
    repo = TurnRepository(db_session)
    user = await repo.create(
        campaign.id,
        TurnCreate(role="user", content="Я вхожу в комнату."),
    )
    assistant = await repo.create(
        campaign.id,
        TurnCreate(
            role="assistant",
            content="Ты входишь в комнату.",
            parent_turn_id=user.id,
        ),
    )
    meta_user = await repo.create(
        campaign.id,
        TurnCreate(role="meta_user", content="/DM Всё корректно?"),
    )
    meta_assistant = await repo.create(
        campaign.id,
        TurnCreate(
            role="meta_assistant",
            content="Да.",
            parent_turn_id=meta_user.id,
        ),
    )
    await db_session.commit()

    assert await repo.undo_last_pair(campaign.id) is True
    await db_session.commit()

    all_turns = await repo.get_history(
        campaign.id,
        active_only=False,
        channel="all",
    )
    status = {turn.id: turn.status for turn in all_turns}
    assert status[user.id] == "undone"
    assert status[assistant.id] == "undone"
    assert status[meta_user.id] == "active"
    assert status[meta_assistant.id] == "active"
