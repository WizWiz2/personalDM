"""F-36: registrar must not recreate a same-turn authority NPC under an abbreviated role."""
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.location_repo import LocationRepository
from app.db.repositories.scene_repo import SceneRepository
from app.models.campaign import CampaignCreate
from app.models.character import CharacterCreate
from app.models.location import LocationCreate
from app.models.scene import SceneCreate
from app.services.entity_registrar import EntityRegistrar


@pytest.mark.asyncio
async def test_same_turn_generic_groom_reuses_structured_groom(db_session):
    cid, source_turn = uuid4(), uuid4()
    await CampaignRepository(db_session).create(cid, CampaignCreate(name="F-36 groom"))
    loc = await LocationRepository(db_session).create(
        cid, LocationCreate(canonical_name="Дозорная ферма"),
    )
    scene = await SceneRepository(db_session).create(
        cid, SceneCreate(title="Во дворе", location_id=loc.id),
    )
    entities = EntityRepository(db_session)
    groom = await entities.create_character(
        cid, CharacterCreate(
            canonical_name="Конюх дозорной фермы",
            current_location_id=loc.id,
            custom_fields={
                "introduced_by": "turn_authority",
                "introduction_turn_id": str(source_turn),
                "role": "конюх дозорной фермы",
                "temporary_name": True,
            },
        ),
    )
    await SceneRepository(db_session).add_participant(scene.id, groom.id)
    text = "Конюх выходит во двор и машет рукой."
    registrar = EntityRegistrar(db_session)
    registrar._router.resolve = AsyncMock(return_value=object())
    registrar._router.generate_json = AsyncMock(return_value={"characters": [{
        "canonical_name": "Конюх",
        "role": "конюх",
        "temporary_name": True,
        "presence": "present",
        "evidence": "Конюх выходит во двор",
    }]})

    result = await registrar.register_from_turn(
        cid, scene.id, source_turn, text, promotion_only=False,
    )
    assert result.created_ids == []
    assert groom.id in result.resolved_ids
    assert len(await entities.list_by_campaign(cid, "character")) == 1
