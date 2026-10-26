"""Replay15 k3: redoing an undone trip reused the undone (inactive) place and left the hero in it."""
from uuid import uuid4

import pytest

from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.location_repo import LocationRepository
from app.db.tables import Entity
from app.models.campaign import CampaignCreate
from app.models.location import LocationCreate
from app.services.scene_transition_executor import SceneTransitionExecutor


@pytest.mark.asyncio
async def test_a_redo_reactivates_the_place_an_undo_retired(db_session):
    campaign_id = uuid4()
    await CampaignRepository(db_session).create(campaign_id, CampaignCreate(name="Redo"))
    inn = await LocationRepository(db_session).create(campaign_id, LocationCreate(canonical_name="Трактир"))
    (await db_session.get(Entity, str(inn.id))).status = "inactive"

    place_id, created = await SceneTransitionExecutor(db_session)._resolve_or_create_location(
        campaign_id, "Трактир", None)

    assert (place_id, created) == (inn.id, False)
    assert (await db_session.get(Entity, str(inn.id))).status == "active"
