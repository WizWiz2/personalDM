"""F-34: regular conversation cannot consume narrative hours or dawn deadlines."""
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import Campaign, Scene
from app.services.scene_state_service import SceneStateService


@pytest.mark.asyncio
async def test_published_dialogue_does_not_turn_night_into_dawn(db_session: AsyncSession):
    cid, sid = uuid4(), uuid4()
    db_session.add(Campaign(id=str(cid), name="F-34 clock"))
    db_session.add(Scene(id=str(sid), campaign_id=str(cid), title="Ночной разговор"))
    await db_session.flush()
    state = SceneStateService(db_session)
    runtime = await state.ensure_runtime_state(sid)
    runtime.world_time_label = "night"
    runtime.world_time_order = 0
    await db_session.flush()
    for _ in range(50):
        assert await state.advance_published_turn(sid) is None
    assert runtime.world_time_label == "night"
    assert runtime.world_time_order == 50
