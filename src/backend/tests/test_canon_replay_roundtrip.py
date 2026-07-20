import json
from datetime import datetime
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import Campaign, Character, Entity, Item, ProposedChange, Turn
from app.models.proposed_change import ChangeType
from app.services.campaign_archive import CampaignArchiveService
from app.services.canon_applier import CanonApplier
from app.services.initial_world_state import InitialWorldStateService


async def _add_proposal(
    session: AsyncSession,
    campaign_id: UUID,
    change_type: ChangeType,
    payload: dict,
) -> tuple[UUID, str]:
    turn_id = uuid4()
    proposal_id = str(uuid4())
    session.add(
        Turn(
            id=str(turn_id),
            campaign_id=str(campaign_id),
            role="assistant",
            content=f"Confirmed {change_type.value}",
        )
    )
    session.add(
        ProposedChange(
            id=proposal_id,
            turn_id=str(turn_id),
            change_type=change_type.value,
            payload=json.dumps(payload, ensure_ascii=False),
            status="accepted",
            resolved_at=datetime.utcnow(),
        )
    )
    await session.flush()
    return turn_id, proposal_id


@pytest.mark.asyncio
async def test_rebuild_replays_movement_and_item_transfer_without_drift(
    db_session: AsyncSession,
    tmp_path,
):
    campaign_id = uuid4()
    hero_id = uuid4()
    item_id = uuid4()
    camp_id = uuid4()
    archive_id = uuid4()

    db_session.add(Campaign(id=str(campaign_id), name="Replay"))
    db_session.add_all(
        [
            Entity(
                id=str(hero_id),
                campaign_id=str(campaign_id),
                entity_type="character",
                canonical_name="Элдон",
            ),
            Entity(
                id=str(item_id),
                campaign_id=str(campaign_id),
                entity_type="item",
                canonical_name="Ключ архива",
            ),
            Entity(
                id=str(camp_id),
                campaign_id=str(campaign_id),
                entity_type="location",
                canonical_name="Лагерь",
            ),
            Entity(
                id=str(archive_id),
                campaign_id=str(campaign_id),
                entity_type="location",
                canonical_name="Архив",
            ),
        ]
    )
    db_session.add(
        Character(entity_id=str(hero_id), current_location_id=str(camp_id))
    )
    db_session.add(Item(entity_id=str(item_id), current_owner_id=str(hero_id)))
    await db_session.flush()

    movement_payload = {
        "character_id": str(hero_id),
        "location_id": str(archive_id),
        "description": "Элдон вошёл в архив.",
    }
    movement_turn_id, movement_proposal_id = await _add_proposal(
        db_session,
        campaign_id,
        ChangeType.MOVEMENT,
        movement_payload,
    )
    await CanonApplier(db_session).apply(
        campaign_id,
        ChangeType.MOVEMENT,
        movement_payload,
        movement_turn_id,
    )
    await db_session.commit()

    snapshot = await InitialWorldStateService(db_session).get_snapshot(campaign_id)
    assert snapshot is not None
    assert snapshot["baseline_proposal_ids"] == []
    assert snapshot["characters"][str(hero_id)]["current_location_id"] == str(camp_id)
    assert snapshot["items"][str(item_id)]["current_owner_id"] == str(hero_id)
    assert movement_proposal_id not in snapshot["baseline_proposal_ids"]

    transfer_payload = {
        "item_id": str(item_id),
        "owner_id": None,
        "location_id": str(archive_id),
        "description": "Ключ оставлен в архиве.",
    }
    transfer_turn_id, _ = await _add_proposal(
        db_session,
        campaign_id,
        ChangeType.ITEM_TRANSFER,
        transfer_payload,
    )
    await CanonApplier(db_session).apply(
        campaign_id,
        ChangeType.ITEM_TRANSFER,
        transfer_payload,
        transfer_turn_id,
    )
    await db_session.commit()

    expected_state = await InitialWorldStateService(db_session).current_projection(campaign_id)
    fake_backup = tmp_path / "before-rebuild.db"
    with patch(
        "app.services.campaign_archive.backup_database",
        return_value=fake_backup,
    ):
        report = await CampaignArchiveService(db_session).rebuild_canon(
            campaign_id,
            apply=True,
        )

    assert report["applied"] is True
    assert report["verified"] is True
    assert report["stateful_replay_proposals"] == 2
    assert report["baseline_covered_stateful_proposals"] == 0
    assert report["verification_differences"] == []
    assert report["projection_hash_before"] == report["projection_hash_after"]
    assert await InitialWorldStateService(db_session).current_projection(campaign_id) == expected_state


@pytest.mark.asyncio
async def test_legacy_checkpoint_marks_existing_stateful_proposals_as_baseline(
    db_session: AsyncSession,
):
    campaign_id = uuid4()
    hero_id = uuid4()
    location_id = uuid4()
    db_session.add(Campaign(id=str(campaign_id), name="Legacy"))
    db_session.add_all(
        [
            Entity(
                id=str(hero_id),
                campaign_id=str(campaign_id),
                entity_type="character",
                canonical_name="Старый герой",
            ),
            Entity(
                id=str(location_id),
                campaign_id=str(campaign_id),
                entity_type="location",
                canonical_name="Башня",
            ),
        ]
    )
    db_session.add(
        Character(entity_id=str(hero_id), current_location_id=str(location_id))
    )
    _, proposal_id = await _add_proposal(
        db_session,
        campaign_id,
        ChangeType.MOVEMENT,
        {
            "character_id": str(hero_id),
            "location_id": str(location_id),
            "description": "Герой уже находится в башне.",
        },
    )
    await db_session.commit()

    snapshot = await InitialWorldStateService(db_session).ensure_snapshot(campaign_id)
    await db_session.commit()
    assert proposal_id in snapshot["baseline_proposal_ids"]

    report = await CampaignArchiveService(db_session).rebuild_canon(
        campaign_id,
        apply=False,
    )
    assert report["checkpoint_exists"] is True
    assert report["baseline_covered_stateful_proposals"] == 1
    assert report["stateful_replay_proposals"] == 1


@pytest.mark.asyncio
async def test_archive_v2_contains_checkpoint_and_verifiable_hash(
    db_session: AsyncSession,
    tmp_path,
    monkeypatch,
):
    campaign_id = uuid4()
    db_session.add(Campaign(id=str(campaign_id), name="Archive v2"))
    await db_session.commit()
    await InitialWorldStateService(db_session).ensure_snapshot(campaign_id)
    await db_session.commit()

    monkeypatch.setattr("app.services.campaign_archive.settings.DATA_DIR", str(tmp_path))
    service = CampaignArchiveService(db_session)
    path, archive = await service.export_json(campaign_id)

    assert path.exists()
    assert archive["format"] == "personal-dm-campaign"
    assert archive["version"] == 2
    assert archive["initial_world_state"]["schema_version"] == 1
    assert archive["integrity"]["canon_projection_hash"] == service._digest(
        archive["canon_projection"]
    )
    unsigned = dict(archive)
    integrity = unsigned.pop("integrity")
    assert integrity["payload_hash"] == service._digest(unsigned)
