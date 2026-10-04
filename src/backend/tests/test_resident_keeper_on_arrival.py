from uuid import uuid4

import pytest

from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.location_repo import LocationRepository
from app.models.campaign import CampaignCreate
from app.models.location import LocationCreate
from app.services.turn_authority_resolvers import NpcIntroductionResolver


@pytest.mark.asyncio
async def test_an_unfilled_resident_slot_authorizes_its_keeper_on_arrival(db_session):
    """B6 T3: arriving at a new inn, «трактирщик у стойки» was judged an absent character."""
    campaign_id = uuid4()
    await CampaignRepository(db_session).create(campaign_id, CampaignCreate(name="Keeper"))
    inn = await LocationRepository(db_session).create(campaign_id, LocationCreate(
        canonical_name="Трактир", custom_fields={"resident_role": "трактирщик"}))

    resolved = await NpcIntroductionResolver(db_session).resolve(
        campaign_id=campaign_id, introductions=[], present_names=[], target_location_id=inn.id)

    [keeper] = resolved.new_introductions
    assert (keeper.canonical_name, keeper.resident_slot) == ("Трактирщик", str(inn.id))
