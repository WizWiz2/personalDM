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


@pytest.mark.asyncio
async def test_a_step_that_brings_a_known_person_moves_them_here(db_session):
    """A10 T9-T11: the servant sent to the post office could never be typed as coming back."""
    from app.db.repositories.entity_repo import EntityRepository
    from app.models.character import CharacterCreate

    campaign_id = uuid4()
    await CampaignRepository(db_session).create(campaign_id, CampaignCreate(name="Errand"))
    places = LocationRepository(db_session)
    inn = await places.create(campaign_id, LocationCreate(canonical_name="Трактир"))
    post = await places.create(campaign_id, LocationCreate(canonical_name="Почтовый двор"))
    servant = await EntityRepository(db_session).create_character(campaign_id, CharacterCreate(
        canonical_name="Служащий", current_location_id=post.id, custom_fields={"temporary_name": True}))
    back = PlannedNpcIntroduction(canonical_name="Служащий", role="служащий", temporary_name=True,
                                  reason="Вернулся с поручения.", after_action_index=0, arrives=True)

    resolved = await NpcIntroductionResolver(db_session).resolve(
        campaign_id=campaign_id, introductions=[back], present_names=[], target_location_id=inn.id)

    assert [item.entity_id for item in resolved.existing_arrivals] == [servant.id]
    assert not resolved.new_introductions
    # Merely found here, a designation is local: another «Служащий», never the one elsewhere.
    found = await NpcIntroductionResolver(db_session).resolve(
        campaign_id=campaign_id, introductions=[back.model_copy(update={"arrives": False})],
        present_names=[], target_location_id=inn.id)
    assert not found.existing_arrivals
