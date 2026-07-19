import json
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.turn_repo import TurnRepository
from app.db.tables import Character, Item, ProposedChange
from app.models.campaign import CampaignCreate
from app.models.character import CharacterCreate
from app.models.entity import EntityCreate, EntityType
from app.models.proposed_change import ChangeType
from app.models.turn import TurnCreate
from app.services.campaign_archive import CampaignArchiveService
from app.services.canon_applier import CanonApplier
from app.services.initial_world_state import InitialWorldStateService


@pytest.mark.asyncio
async def test_stateful_rebuild_restores_initial_state_and_replays_all_deltas(db_session):
    campaign_id = uuid4()
    await CampaignRepository(db_session).create(
        campaign_id,
        CampaignCreate(name="Replay campaign"),
    )
    entities = EntityRepository(db_session)
    camp = await entities.create(
        campaign_id,
        EntityCreate(entity_type=EntityType.LOCATION, canonical_name="Camp"),
    )
    archive = await entities.create(
        campaign_id,
        EntityCreate(entity_type=EntityType.LOCATION, canonical_name="Archive"),
    )
    hero = await entities.create_character(
        campaign_id,
        CharacterCreate(canonical_name="Eldon", current_location_id=camp.id),
    )
    key = await entities.create(
        campaign_id,
        EntityCreate(entity_type=EntityType.ITEM, canonical_name="Brass key"),
    )
    db_session.add(Item(entity_id=str(key.id), current_owner_id=str(hero.id)))

    turn = await TurnRepository(db_session).create(
        campaign_id,
        TurnCreate(role="assistant", content="Eldon enters the archive and leaves the key there."),
    )
    movement_payload = {
        "character_id": str(hero.id),
        "location_id": str(archive.id),
        "description": "Eldon enters the archive.",
    }
    item_payload = {
        "item_id": str(key.id),
        "owner_id": None,
        "location_id": str(archive.id),
        "description": "The key remains in the archive.",
    }
    db_session.add_all(
        [
            ProposedChange(
                turn_id=str(turn.id),
                change_type=ChangeType.MOVEMENT.value,
                payload=json.dumps(movement_payload),
                status="accepted",
            ),
            ProposedChange(
                turn_id=str(turn.id),
                change_type=ChangeType.ITEM_TRANSFER.value,
                payload=json.dumps(item_payload),
                status="accepted",
            ),
        ]
    )
    await db_session.flush()

    applier = CanonApplier(db_session)
    await applier.apply(campaign_id, ChangeType.MOVEMENT, movement_payload, turn.id)
    await applier.apply(campaign_id, ChangeType.ITEM_TRANSFER, item_payload, turn.id)
    await db_session.commit()

    initial = await InitialWorldStateService(db_session).get(campaign_id)
    assert initial is not None
    initial_character = initial["snapshot"]["characters"][0]
    initial_item = initial["snapshot"]["items"][0]
    assert initial_character["current_location_id"] == str(camp.id)
    assert initial_item["current_owner_id"] == str(hero.id)

    with patch(
        "app.services.campaign_archive.backup_database",
        return_value=Path("/tmp/replay-backup.db"),
    ):
        report = await CampaignArchiveService(db_session).rebuild_canon(
            campaign_id,
            apply=True,
            verify=True,
        )

    assert report["applied"] is True
    assert report["verified"] is True
    assert report["matches_previous_state"] is True
    assert report["stateful_proposals"] == 2
    assert report["skipped"] == []

    character_location = (
        await db_session.execute(
            select(Character.current_location_id).where(Character.entity_id == str(hero.id))
        )
    ).scalar_one()
    item_state = (
        await db_session.execute(
            select(Item.current_owner_id, Item.current_location_id).where(
                Item.entity_id == str(key.id)
            )
        )
    ).one()
    assert character_location == str(archive.id)
    assert item_state.current_owner_id is None
    assert item_state.current_location_id == str(archive.id)


@pytest.mark.asyncio
async def test_export_contains_versioned_initial_world_state(db_session, tmp_path):
    campaign_id = uuid4()
    await CampaignRepository(db_session).create(
        campaign_id,
        CampaignCreate(name="Round trip"),
    )
    await InitialWorldStateService(db_session).capture(campaign_id)

    with patch("app.services.campaign_archive.settings.DATA_DIR", str(tmp_path)):
        path, exported = await CampaignArchiveService(db_session).export_json(campaign_id)

    assert path.exists()
    assert exported["archive"]["format"] == "personal-dm-campaign"
    assert exported["archive"]["version"] == 2
    assert exported["archive"]["initial_world_state"]["snapshot_hash"]
