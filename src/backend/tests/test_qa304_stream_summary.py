"""The final response chunk must not outrun its durable performance receipt."""
import json

import pytest
from sqlalchemy import select

from app.db.tables import GenerationDecision
from app.models.turn import TurnCreate
from app.services.turn_saga import TurnSaga
from tests.test_turn_saga_world_move import _seams, _world


@pytest.mark.asyncio
async def test_summary_is_committed_before_final_chunk_even_if_reader_stops(file_db, monkeypatch):
    async with file_db() as session:
        cid, scene, npc, _, pier = await _world(session)
    _seams(monkeypatch, npc, pier)
    async with file_db() as session:
        stream = TurnSaga(session).run_turn_stream(cid, TurnCreate(role="user", content="Жду.", scene_id=scene))
        try:
            result = await anext(stream)
            assert result == "Йонас кивает и уходит к пристани."
            async with file_db() as observer:
                rows = (await observer.execute(select(GenerationDecision).where(
                    GenerationDecision.campaign_id == str(cid), GenerationDecision.step == "performance",
                ))).scalars().all()
                assert len(rows) == 1
                assert json.loads(rows[0].payload_json)["status"] == "published"
        finally:
            await stream.aclose()
