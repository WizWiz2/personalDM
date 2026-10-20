"""F-36: moving from the road into a forge must not create the same blacksmith twice."""
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.location_repo import LocationRepository
from app.models.campaign import CampaignCreate
from app.models.character import CharacterCreate
from app.models.location import LocationCreate
from app.models.turn_authority import PlannedNpcIntroduction
from app.services.turn_authority_resolvers import NpcIntroductionResolver


@pytest.mark.asyncio
async def test_unique_temporary_blacksmith_in_parent_location_is_reused_in_forge(
    db_session: AsyncSession,
):
    cid = uuid4()
    await CampaignRepository(db_session).create(cid, CampaignCreate(name="QA-213 blacksmith"))
    locations = LocationRepository(db_session)
    road = await locations.create(cid, LocationCreate(canonical_name="Кузнечная дорога"))
    forge = await locations.create(cid, LocationCreate(
        canonical_name="Кузница Усты", parent_location_id=road.id,
    ))
    keeper = await EntityRepository(db_session).create_character(
        cid,
        CharacterCreate(
            canonical_name="Местный кузнец", current_location_id=road.id,
            custom_fields={"temporary_name": True, "role": "местный кузнец"},
        ),
    )
    planned = PlannedNpcIntroduction(
        canonical_name="Кузнец", role="кузнец", temporary_name=True,
        reason="Кузнец у горна отвечает на обращение игрока.",
    )
    result = await NpcIntroductionResolver(db_session).resolve(
        campaign_id=cid, introductions=[planned], present_names=[],
        target_location_id=forge.id, addressee="Кузнец",
    )
    assert result.new_introductions == []
    assert len(result.existing_arrivals) == 1
    assert result.existing_arrivals[0].entity_id == keeper.id
    assert result.addressee == keeper.canonical_name
