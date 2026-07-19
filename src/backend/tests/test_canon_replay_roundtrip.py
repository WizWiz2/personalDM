import json
from pathlib import Path
from uuid import UUID, uuid4
from unittest.mock import patch

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import (
    Campaign,
    Character,
    Belief,
    Entity,
    Event,
    Fact,
    Item,
    ProposedChange,
    ProviderConfig,
    Turn,
    WorldStateSnapshot,
)
from app.models.proposed_change import ChangeType
from app.services.campaign_archive import ARCHIVE_VERSION, CampaignArchiveService
from app.services.canon_applier import CanonApplier
from app.services.world_state_snapshot import WorldStateSnapshotService


async def _build_stateful_campaign(session: AsyncSession) -> dict[str, UUID]:
    campaign_id = uuid4()
    hero_id = uuid4()
    item_id = uuid4()
    camp_id = uuid4()
    archive_id = uuid4()
    turn_id = uuid4()

    session.add(Campaign(id=str(campaign_id), name="Replay campaign"))
    session.add_all(
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
                canonical_name="Латунный ключ",
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
    await session.flush()
    session.add(Character(entity_id=str(hero_id), current_location_id=str(camp_id)))
    session.add(Item(entity_id=str(item_id), current_owner_id=str(hero_id)))
    session.add(
        ProviderConfig(
            campaign_id=str(campaign_id),
            base_url="http://127.0.0.1:11434",
            model_name="gemma",
            api_key_encrypted="must-not-leave-the-database",
            context_window=8192,
        )
    )
    session.add(
        Turn(
            id=str(turn_id),
            campaign_id=str(campaign_id),
            role="assistant",
            content="Элдон входит в архив и кладёт ключ на пьедестал.",
        )
    )
    await session.flush()

    movement_payload = {
        "character_id": str(hero_id),
        "location_id": str(archive_id),
        "description": "Элдон вошёл в архив.",
    }
    transfer_payload = {
        "item_id": str(item_id),
        "owner_id": None,
        "location_id": str(archive_id),
        "description": "Ключ оставлен в архиве.",
    }
    session.add_all(
        [
            ProposedChange(
                turn_id=str(turn_id),
                change_type="movement",
                payload=json.dumps(movement_payload, ensure_ascii=False),
                status="accepted",
            ),
            ProposedChange(
                turn_id=str(turn_id),
                change_type="item_transfer",
                payload=json.dumps(transfer_payload, ensure_ascii=False),
                status="accepted",
            ),
        ]
    )
    await session.flush()

    applier = CanonApplier(session)
    await applier.apply(campaign_id, ChangeType.MOVEMENT, movement_payload, turn_id)
    await applier.apply(campaign_id, ChangeType.ITEM_TRANSFER, transfer_payload, turn_id)
    await session.commit()
    return {
        "campaign_id": campaign_id,
        "hero_id": hero_id,
        "item_id": item_id,
        "camp_id": camp_id,
        "archive_id": archive_id,
        "turn_id": turn_id,
    }


@pytest.mark.asyncio
async def test_stateful_changes_capture_initial_snapshot_and_rebuild_exactly(
    db_session: AsyncSession,
):
    ids = await _build_stateful_campaign(db_session)
    campaign_id = ids["campaign_id"]

    snapshot = await WorldStateSnapshotService(db_session).get(campaign_id)
    assert snapshot is not None
    assert snapshot["characters"] == [
        {
            "entity_id": str(ids["hero_id"]),
            "current_location_id": str(ids["camp_id"]),
        }
    ]
    assert snapshot["items"] == [
        {
            "entity_id": str(ids["item_id"]),
            "current_owner_id": str(ids["hero_id"]),
            "current_location_id": None,
        }
    ]

    manual_event = Event(
        campaign_id=str(campaign_id),
        event_type="manual_note",
        description="Ручная заметка до replay.",
        source_turns="[]",
    )
    db_session.add(manual_event)
    await db_session.commit()

    archive = CampaignArchiveService(db_session)
    dry_run = await archive.rebuild_canon(campaign_id)
    assert dry_run["stateful_proposals"] == 2
    assert dry_run["replayable_proposals"] == 2
    assert dry_run["skipped"] == []
    assert dry_run["has_initial_world_snapshot"] is True

    with patch(
        "app.services.campaign_archive.backup_database",
        return_value=Path("/tmp/replay-backup.db"),
    ):
        rebuilt = await archive.rebuild_canon(campaign_id, apply=True)

    assert rebuilt["applied"] is True
    assert rebuilt["semantic_match_before"] is True
    character = await db_session.get(Character, str(ids["hero_id"]))
    item = await db_session.get(Item, str(ids["item_id"]))
    assert character.current_location_id == str(ids["archive_id"])
    assert item.current_owner_id is None
    assert item.current_location_id == str(ids["archive_id"])
    events = (
        (await db_session.execute(select(Event).where(Event.campaign_id == str(campaign_id))))
        .scalars()
        .all()
    )
    assert {event.event_type for event in events} == {
        "manual_note",
        "movement",
        "item_transfer",
    }


@pytest.mark.asyncio
async def test_export_delete_import_round_trip_preserves_state_and_scrubs_secret(
    db_session: AsyncSession,
):
    ids = await _build_stateful_campaign(db_session)
    campaign_id = ids["campaign_id"]
    service = CampaignArchiveService(db_session)
    archive = await service.build_archive(campaign_id)

    assert archive["archive_version"] == ARCHIVE_VERSION
    assert archive["archive_digest"]
    assert archive["state_digest"]
    assert archive["tables"]["provider_configs"][0]["api_key_encrypted"] is None
    assert len(archive["tables"]["world_state_snapshots"]) == 1

    await db_session.execute(delete(Campaign).where(Campaign.id == str(campaign_id)))
    await db_session.commit()
    assert await db_session.get(Campaign, str(campaign_id)) is None

    imported = await service.import_archive(archive, replace=True)
    assert imported["campaign_id"] == str(campaign_id)
    assert imported["state_matches_export"] is True
    assert imported["archive_digest"] == archive["archive_digest"]
    assert imported["inserted"]["proposed_changes"] == 2

    restored_character = await db_session.get(Character, str(ids["hero_id"]))
    restored_item = await db_session.get(Item, str(ids["item_id"]))
    restored_snapshot = await db_session.scalar(
        select(WorldStateSnapshot).where(WorldStateSnapshot.campaign_id == str(campaign_id))
    )
    provider = await db_session.scalar(
        select(ProviderConfig).where(ProviderConfig.campaign_id == str(campaign_id))
    )
    assert restored_character.current_location_id == str(ids["archive_id"])
    assert restored_item.current_location_id == str(ids["archive_id"])
    assert restored_snapshot is not None
    assert provider.api_key_encrypted is None

    exported_again = await service.build_archive(campaign_id)
    assert exported_again["archive_digest"] == archive["archive_digest"]
    assert exported_again["state_digest"] == archive["state_digest"]


@pytest.mark.asyncio
async def test_import_rejects_tampered_or_secret_archive(db_session: AsyncSession):
    ids = await _build_stateful_campaign(db_session)
    archive = await CampaignArchiveService(db_session).build_archive(ids["campaign_id"])

    tampered = json.loads(json.dumps(archive))
    tampered["tables"]["campaigns"][0]["name"] = "Подмена"
    with pytest.raises(ValueError, match="digest"):
        await CampaignArchiveService(db_session).import_archive(tampered, replace=True)

    secret = json.loads(json.dumps(archive))
    secret["tables"]["provider_configs"][0]["api_key_encrypted"] = "leak"
    archive_module = __import__("app.services.campaign_archive", fromlist=["_archive_digest"])
    secret["archive_digest"] = archive_module._archive_digest(
        campaign_id=secret["campaign_id"],
        state_digest=secret["state_digest"],
        tables=secret["tables"],
    )
    with pytest.raises(ValueError, match="secrets"):
        await CampaignArchiveService(db_session).import_archive(secret, replace=True)


@pytest.mark.asyncio
async def test_snapshot_extends_for_character_created_after_initial_capture(
    db_session: AsyncSession,
):
    ids = await _build_stateful_campaign(db_session)
    campaign_id = ids["campaign_id"]
    late_character_id = uuid4()
    late_turn_id = uuid4()
    db_session.add(
        Entity(
            id=str(late_character_id),
            campaign_id=str(campaign_id),
            entity_type="character",
            canonical_name="Поздний спутник",
        )
    )
    await db_session.flush()
    db_session.add(
        Character(
            entity_id=str(late_character_id),
            current_location_id=str(ids["camp_id"]),
        )
    )
    db_session.add(
        Turn(
            id=str(late_turn_id),
            campaign_id=str(campaign_id),
            role="assistant",
            content="Спутник вошёл в архив.",
        )
    )
    await db_session.flush()
    payload = {
        "character_id": str(late_character_id),
        "location_id": str(ids["archive_id"]),
        "description": "Спутник вошёл в архив.",
    }
    db_session.add(
        ProposedChange(
            turn_id=str(late_turn_id),
            change_type="movement",
            payload=json.dumps(payload, ensure_ascii=False),
            status="accepted",
        )
    )
    await db_session.flush()
    await CanonApplier(db_session).apply(campaign_id, ChangeType.MOVEMENT, payload, late_turn_id)
    await db_session.commit()

    snapshot = await WorldStateSnapshotService(db_session).get(campaign_id)
    late_baseline = next(
        row for row in snapshot["characters"] if row["entity_id"] == str(late_character_id)
    )
    assert late_baseline["current_location_id"] == str(ids["camp_id"])

    with pytest.raises(ValueError, match="source_turn_id"):
        await WorldStateSnapshotService(db_session).assert_manual_mutation_allowed(campaign_id)


@pytest.mark.asyncio
async def test_rebuild_remaps_knowledge_to_recreated_fact(db_session: AsyncSession):
    campaign_id = uuid4()
    character_id = uuid4()
    fact_turn_id = uuid4()
    knowledge_turn_id = uuid4()
    db_session.add(Campaign(id=str(campaign_id), name="Fact reference replay"))
    db_session.add(
        Entity(
            id=str(character_id),
            campaign_id=str(campaign_id),
            entity_type="character",
            canonical_name="Ария",
        )
    )
    await db_session.flush()
    db_session.add(Character(entity_id=str(character_id)))
    db_session.add_all(
        [
            Turn(
                id=str(fact_turn_id),
                campaign_id=str(campaign_id),
                role="assistant",
                content="Дверь открыта.",
            ),
            Turn(
                id=str(knowledge_turn_id),
                campaign_id=str(campaign_id),
                role="assistant",
                content="Ария замечает открытую дверь.",
            ),
        ]
    )
    await db_session.flush()
    fact_payload = {
        "subject": "дверь",
        "predicate": "состояние",
        "object_value": "открыта",
    }
    fact_proposal = ProposedChange(
        turn_id=str(fact_turn_id),
        change_type="fact",
        payload=json.dumps(fact_payload, ensure_ascii=False),
        status="accepted",
    )
    db_session.add(fact_proposal)
    await db_session.flush()
    await CanonApplier(db_session).apply(campaign_id, ChangeType.FACT, fact_payload, fact_turn_id)
    original_fact = await db_session.scalar(
        select(Fact).where(Fact.campaign_id == str(campaign_id))
    )
    knowledge_payload = {
        "recipient_id": str(character_id),
        "fact_id": original_fact.id,
        "proposition": "Дверь открыта.",
        "confidence": 1.0,
    }
    db_session.add(
        ProposedChange(
            turn_id=str(knowledge_turn_id),
            change_type="knowledge",
            payload=json.dumps(knowledge_payload, ensure_ascii=False),
            status="accepted",
        )
    )
    await db_session.flush()
    await CanonApplier(db_session).apply(
        campaign_id, ChangeType.KNOWLEDGE, knowledge_payload, knowledge_turn_id
    )
    await db_session.commit()

    with patch(
        "app.services.campaign_archive.backup_database",
        return_value=Path("/tmp/fact-reference-replay.db"),
    ):
        report = await CampaignArchiveService(db_session).rebuild_canon(campaign_id, apply=True)

    rebuilt_fact = await db_session.scalar(select(Fact).where(Fact.campaign_id == str(campaign_id)))
    belief = await db_session.scalar(select(Belief).where(Belief.character_id == str(character_id)))
    assert report["semantic_match_before"] is True
    assert rebuilt_fact.id != original_fact.id
    assert belief.fact_id == rebuilt_fact.id


@pytest.mark.asyncio
async def test_failed_import_rolls_back_all_rows(db_session: AsyncSession):
    ids = await _build_stateful_campaign(db_session)
    campaign_id = ids["campaign_id"]
    service = CampaignArchiveService(db_session)
    archive = await service.build_archive(campaign_id)
    await service._purge_campaign_rows(str(campaign_id))
    await db_session.commit()

    archive["state_digest"] = "0" * 64
    archive_module = __import__("app.services.campaign_archive", fromlist=["_archive_digest"])
    archive["archive_digest"] = archive_module._archive_digest(
        campaign_id=archive["campaign_id"],
        state_digest=archive["state_digest"],
        tables=archive["tables"],
    )
    with pytest.raises(ValueError, match="state digest"):
        await service.import_archive(archive)

    assert await db_session.get(Campaign, str(campaign_id)) is None
    assert (
        await db_session.scalar(select(Entity.id).where(Entity.campaign_id == str(campaign_id)))
        is None
    )
