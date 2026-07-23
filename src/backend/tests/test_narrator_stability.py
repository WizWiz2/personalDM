import json
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.repositories.turn_repo import TurnRepository
from app.db.tables import ProposedChange
from app.models.campaign import CampaignCreate
from app.models.scene import SceneCreate
from app.models.turn import TurnCreate
from app.services.context_compiler import ContextCompiler


async def add_pair(
    session: AsyncSession,
    campaign_id,
    scene_id,
    number: int,
    *,
    progress: str | None = None,
):
    turns = TurnRepository(session)
    user = await turns.create(
        campaign_id,
        TurnCreate(role="user", content=f"Попытка {number}", scene_id=scene_id),
    )
    assistant = await turns.create(
        campaign_id,
        TurnCreate(
            role="assistant",
            content=f"Ответ ДМа {number}",
            scene_id=scene_id,
            parent_turn_id=user.id,
        ),
    )
    if progress:
        session.add(
            ProposedChange(
                turn_id=str(assistant.id),
                change_type="event",
                payload=json.dumps({"description": progress}, ensure_ascii=False),
                status="accepted",
            )
        )
    await session.flush()
    return assistant


@pytest.mark.asyncio
async def test_narrator_receipt_replaces_long_history_with_structured_progress(
    db_session: AsyncSession,
    monkeypatch,
):
    monkeypatch.setattr(settings, "NARRATOR_HISTORY_LIMIT", 4)
    campaign_id = uuid4()
    await CampaignRepository(db_session).create(
        campaign_id,
        CampaignCreate(name="Focused Gemma"),
    )
    scene = await SceneRepository(db_session).create(
        campaign_id,
        SceneCreate(title="Башня"),
    )
    for number in range(1, 8):
        await add_pair(
            db_session,
            campaign_id,
            scene.id,
            number,
            progress=("Каменная дверь открылась" if number == 7 else None),
        )
    await db_session.commit()

    messages, metadata = await ContextCompiler(db_session).compile_context(
        campaign_id=campaign_id,
        scene_id=scene.id,
    )
    context = "\n".join(message.content for message in messages)

    assert "Каменная дверь открылась" in context
    assert "Continue from these consequences" in context
    assert metadata["scene_receipt_items"] == 1
    assert metadata["history_turns_count"] == 4
    assert metadata["narrator_history_limit"] == 4
    assert metadata["stagnation_detected"] is False


@pytest.mark.asyncio
async def test_watchdog_activates_after_two_turns_without_durable_progress(
    db_session: AsyncSession,
    monkeypatch,
):
    monkeypatch.setattr(settings, "NARRATOR_STAGNATION_TURNS", 2)
    campaign_id = uuid4()
    await CampaignRepository(db_session).create(
        campaign_id,
        CampaignCreate(name="Stagnation Watch"),
    )
    scene = await SceneRepository(db_session).create(
        campaign_id,
        SceneCreate(title="Лестница"),
    )
    await add_pair(db_session, campaign_id, scene.id, 1)
    await add_pair(db_session, campaign_id, scene.id, 2)
    await db_session.commit()

    messages, metadata = await ContextCompiler(db_session).compile_context(
        campaign_id=campaign_id,
        scene_id=scene.id,
    )
    context = "\n".join(message.content for message in messages)

    assert "[Progress Watchdog]" in context
    assert metadata["stagnation_detected"] is True


@pytest.mark.asyncio
async def test_recent_progress_suppresses_watchdog_and_receipt_stays_dm_only(
    db_session: AsyncSession,
    monkeypatch,
):
    monkeypatch.setattr(settings, "NARRATOR_STAGNATION_TURNS", 2)
    campaign_id = uuid4()
    await CampaignRepository(db_session).create(
        campaign_id,
        CampaignCreate(name="No Leakage"),
    )
    scene = await SceneRepository(db_session).create(
        campaign_id,
        SceneCreate(title="Архив"),
    )
    await add_pair(db_session, campaign_id, scene.id, 1)
    await add_pair(
        db_session,
        campaign_id,
        scene.id,
        2,
        progress="Хранитель назвал тайный пароль",
    )
    await db_session.commit()

    narrator_messages, narrator_metadata = await ContextCompiler(
        db_session
    ).compile_context(campaign_id=campaign_id, scene_id=scene.id)
    actor_messages, actor_metadata = await ContextCompiler(db_session).compile_context(
        campaign_id=campaign_id,
        acting_character_id=uuid4(),
        scene_id=scene.id,
        current_user_content="Что ты знаешь?",
    )
    narrator_context = "\n".join(message.content for message in narrator_messages)
    actor_context = "\n".join(message.content for message in actor_messages)

    assert "Хранитель назвал тайный пароль" in narrator_context
    assert narrator_metadata["stagnation_detected"] is False
    assert "Хранитель назвал тайный пароль" not in actor_context
    assert "[Progress Watchdog]" not in actor_context
    assert actor_metadata["scene_receipt_items"] == 0

@pytest.mark.asyncio
async def test_receipt_manifest_only_reports_content_that_was_sent(
    db_session: AsyncSession,
    monkeypatch,
):
    campaign_id = uuid4()
    await CampaignRepository(db_session).create(
        campaign_id,
        CampaignCreate(name="Tight Budget"),
    )
    scene = await SceneRepository(db_session).create(
        campaign_id,
        SceneCreate(title="Tight Budget Scene"),
    )
    await add_pair(
        db_session,
        campaign_id,
        scene.id,
        1,
        progress="Этот прогресс не помещается в prompt",
    )
    await db_session.commit()
    monkeypatch.setattr(settings, "RESPONSE_RESERVE_TOKENS", 4000)

    messages, metadata = await ContextCompiler(db_session).compile_context(
        campaign_id=campaign_id,
        scene_id=scene.id,
    )
    context = "\n".join(message.content for message in messages)

    assert "Этот прогресс не помещается в prompt" not in context
    assert metadata["scene_receipt_items"] == 0
    assert metadata["recent_scene_turns_checked"] == 0
    assert metadata["stagnation_detected"] is False

