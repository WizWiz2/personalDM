"""F-37: a failed player input remains retryable without duplicating its turn."""
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.turns import retry_failed_turn
from app.application.game_application import GameInputRoute
from app.db.tables import Campaign, GenerationRun, Turn


@pytest.mark.asyncio
@pytest.mark.parametrize("generation_status", ["failed", "cancelled"])
async def test_retry_latest_failed_turn_reuses_immutable_player_input(db_session: AsyncSession, generation_status):
    campaign_id, turn_id = uuid4(), uuid4()
    db_session.add(Campaign(id=str(campaign_id), name="F-37 retry"))
    db_session.add(Turn(id=str(turn_id), campaign_id=str(campaign_id),
                        role="user", content="Конюх входит и представляется.",
                        status="failed"))
    db_session.add(GenerationRun(campaign_id=str(campaign_id),
                                 user_turn_id=str(turn_id), status=generation_status))
    await db_session.commit()

    async def chunks():
        yield "ok"

    with patch("app.api.turns.GameApplication") as application:
        application.return_value.route_input = AsyncMock(
            return_value=GameInputRoute(channel="narrative", stream=chunks()),
        )
        response = await retry_failed_turn(campaign_id, turn_id, db_session)
    body = "".join([chunk async for chunk in response.body_iterator])
    assert body == "ok"
    assert (await db_session.get(Turn, str(turn_id))).status == "active"
    kwargs = application.return_value.route_input.await_args.kwargs
    assert kwargs["existing_user_turn_id"] == turn_id
    args = application.return_value.route_input.await_args.args
    assert args[1].content == "Конюх входит и представляется."


@pytest.mark.asyncio
async def test_retry_old_failed_turn_after_newer_input_is_rejected(db_session: AsyncSession):
    campaign_id, failed_id, later_id = uuid4(), uuid4(), uuid4()
    db_session.add(Campaign(id=str(campaign_id), name="F-37 no stale replay"))
    db_session.add(Turn(id=str(failed_id), campaign_id=str(campaign_id),
                        role="user", content="Old input", status="failed"))
    await db_session.flush()
    db_session.add(GenerationRun(campaign_id=str(campaign_id),
                                 user_turn_id=str(failed_id), status="failed"))
    await db_session.flush()
    db_session.add(Turn(id=str(later_id), campaign_id=str(campaign_id),
                        role="user", content="Later action", status="active"))
    await db_session.commit()
    with pytest.raises(HTTPException) as exc:
        await retry_failed_turn(campaign_id, failed_id, db_session)
    assert exc.value.status_code == 409
    assert (await db_session.get(Turn, str(failed_id))).status == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("source_status", ["failed", "active"])
async def test_failed_retry_keeps_source_retryable(db_session, source_status):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.application.game_application import GameApplication

    campaign_id, turn_id = uuid4(), uuid4()
    db_session.add(Campaign(id=str(campaign_id), name="QA-305 retry"))
    db_session.add(Turn(id=str(turn_id), campaign_id=str(campaign_id),
                        role="user", content="Осматриваю комнату.", status=source_status))
    db_session.add(GenerationRun(campaign_id=str(campaign_id),
                                 user_turn_id=str(turn_id), status="failed"))
    await db_session.commit()
    factory = async_sessionmaker(db_session.bind)

    async def bind_current_scene(self, campaign_id, data):
        return data

    with patch.object(GameApplication, "_bind_current_scene", bind_current_scene):
        for _ in range(2):
            async with factory() as request_session:
                response = await retry_failed_turn(campaign_id, turn_id, request_session)
                async for _ in response.body_iterator:
                    pass
            async with factory() as observer:
                assert (await observer.get(Turn, str(turn_id))).status == "failed"


@pytest.mark.asyncio
async def test_only_one_stream_can_retry_the_same_failed_attempt(db_session):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    campaign_id, turn_id = uuid4(), uuid4()
    db_session.add(Campaign(id=str(campaign_id), name="QA-305 retry admission"))
    db_session.add(Turn(id=str(turn_id), campaign_id=str(campaign_id),
                        role="user", content="Осматриваю комнату.", status="failed"))
    db_session.add(GenerationRun(campaign_id=str(campaign_id),
                                 user_turn_id=str(turn_id), status="failed"))
    await db_session.commit()
    factory = async_sessionmaker(db_session.bind)

    async def chunks():
        yield "Ход принят."

    with patch("app.api.turns.GameApplication") as application:
        application.return_value.route_input = AsyncMock(
            side_effect=lambda *args, **kwargs: GameInputRoute(channel="narrative", stream=chunks()),
        )
        async with factory() as first, factory() as second:
            first_response = await retry_failed_turn(campaign_id, turn_id, first)
            second_response = await retry_failed_turn(campaign_id, turn_id, second)
            assert [chunk async for chunk in first_response.body_iterator] == ["Ход принят."]
            assert [chunk async for chunk in second_response.body_iterator] == [
                "Повтор хода недоступен: состояние генерации уже изменилось.",
            ]
