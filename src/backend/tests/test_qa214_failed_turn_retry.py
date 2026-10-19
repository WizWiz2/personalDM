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
async def test_retry_latest_failed_turn_reuses_immutable_player_input(db_session: AsyncSession):
    campaign_id, turn_id = uuid4(), uuid4()
    db_session.add(Campaign(id=str(campaign_id), name="F-37 retry"))
    db_session.add(Turn(id=str(turn_id), campaign_id=str(campaign_id),
                        role="user", content="Конюх входит и представляется.",
                        status="failed"))
    db_session.add(GenerationRun(campaign_id=str(campaign_id),
                                 user_turn_id=str(turn_id), status="failed"))
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
