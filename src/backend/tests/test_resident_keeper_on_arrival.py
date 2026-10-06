from uuid import uuid4

import pytest

from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.location_repo import LocationRepository
from app.models.campaign import CampaignCreate
from app.models.location import LocationCreate
from app.models.turn_authority import PlannedNpcIntroduction
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


@pytest.mark.asyncio
async def test_a_person_found_at_the_keepers_place_is_its_keeper_and_a_brought_one_is_not(db_session):
    """B8 T6: the planner's «Хозяин местного трактира» and the slot's «Трактирщик» became two."""
    campaign_id = uuid4()
    await CampaignRepository(db_session).create(campaign_id, CampaignCreate(name="Keeper"))
    inn = await LocationRepository(db_session).create(campaign_id, LocationCreate(
        canonical_name="Трактир", custom_fields={"resident_role": "трактирщик"}))
    host = PlannedNpcIntroduction(canonical_name="Хозяин трактира", role="хозяин трактира",
                                  temporary_name=True, reason="Стоит за стойкой.", after_action_index=1)
    river = PlannedNpcIntroduction(canonical_name="Речник", role="речник", temporary_name=True,
                                   reason="Хозяин послал за ним.", after_action_index=1, arrives=True)

    resolved = await NpcIntroductionResolver(db_session).resolve(
        campaign_id=campaign_id, introductions=[host, river], present_names=[],
        target_location_id=inn.id)

    assert [(item.canonical_name, item.resident_slot) for item in resolved.new_introductions] == [
        ("Хозяин трактира", str(inn.id)), ("Речник", None)]


@pytest.mark.asyncio
async def test_the_addressee_binds_to_whom_this_turns_designation_resolved(db_session):
    """B9 T8: the host was addressed as the planner's «Хозяин…» beside the slot's «Трактирщик»;
    the grant found no owner and the revealed name never promoted."""
    from app.db.repositories.entity_repo import EntityRepository
    from app.models.character import CharacterCreate

    campaign_id = uuid4()
    await CampaignRepository(db_session).create(campaign_id, CampaignCreate(name="Keeper"))
    inn = await LocationRepository(db_session).create(campaign_id, LocationCreate(
        canonical_name="Трактир", custom_fields={"resident_role": "трактирщик"}))
    await EntityRepository(db_session).create_character(campaign_id, CharacterCreate(
        canonical_name="Трактирщик", current_location_id=inn.id,
        custom_fields={"temporary_name": True, "role": "трактирщик", "slot_id": str(inn.id)}))
    host = PlannedNpcIntroduction(canonical_name="Хозяин и распорядитель трактира",
                                  role="хозяин трактира", temporary_name=True,
                                  reason="Стоит за стойкой.", after_action_index=0)

    resolved = await NpcIntroductionResolver(db_session).resolve(
        campaign_id=campaign_id, introductions=[host], present_names=["Илья", "Трактирщик"],
        target_location_id=inn.id, addressee="Хозяин и распорядитель трактира")

    assert resolved.new_introductions == []
    assert resolved.addressee == "Трактирщик"
