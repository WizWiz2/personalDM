"""F-36: two planned labels for the same typed keeper slot are one NPC."""
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import Campaign
from app.models.turn_authority import PlannedNpcIntroduction
from app.services.turn_authority_resolvers import NpcIntroductionResolver


@pytest.mark.asyncio
async def test_repeated_resident_keeper_intro_reuses_first_planned_identity(
    db_session: AsyncSession,
):
    cid, slot_id = uuid4(), uuid4()
    db_session.add(Campaign(id=str(cid), name="F-36 keeper identity"))
    await db_session.flush()
    introductions = [
        PlannedNpcIntroduction(
            canonical_name="Конюх дозорной фермы",
            role="конюх", temporary_name=True,
            resident_slot=str(slot_id),
            reason="Он встречает игрока у конюшни.",
        ),
        PlannedNpcIntroduction(
            canonical_name="Конюх",
            role="конюх", temporary_name=True,
            resident_slot=str(slot_id),
            reason="Конюх отвечает на вопрос игрока.",
        ),
    ]
    result = await NpcIntroductionResolver(db_session).resolve(
        campaign_id=cid, introductions=introductions,
        present_names=[], target_location_id=None,
    )
    assert len(result.new_introductions) == 1
    assert len(result.present_names) == 1
    assert result.existing_arrivals == []
