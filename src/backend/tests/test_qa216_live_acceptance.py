"""QA-215 production reproductions: F-44, F-45 and post-replay F-39."""
from uuid import uuid4
from unittest.mock import AsyncMock, patch

import pytest

from app.models.proposed_change import ChangeType
from app.services.memory_scribe import MemoryScribe
from app.services.narration_meta_guard import leaked_meta_lines
from app.services.turn_undo_service import TurnUndoService
from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.location_repo import LocationRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.tables import Character
from app.models.campaign import CampaignCreate, CampaignUpdate
from app.models.character import CharacterCreate
from app.models.location import LocationCreate
from app.models.scene import SceneCreate


def test_f44_owner_and_scene_is_valid_single_custody():
    owner, location, item = (uuid4() for _ in range(3))
    known = {"вера": str(owner), "архив": str(location), "письмо": str(item)}
    result = MemoryScribe(None)._normalize_payload(
        ChangeType.ITEM_TRANSFER,
        {"item_id": "письмо", "owner_id": "Вера", "location_id": "Архив",
         "description": "Вера получила письмо в архиве."},
        known, set(known.values()), None, None, [], authoritative_text="Вера получила письмо в архиве.",
    )
    assert result is not None
    assert result["item_id"] == str(item)
    assert result["owner_id"] == str(owner)
    # Item location is implicit in holder location; no contradictory double custody.
    assert result["location_id"] is None


def test_f44_unknown_destination_does_not_erase_known_item_location():
    item = uuid4()
    result = MemoryScribe(None)._normalize_payload(
        ChangeType.ITEM_TRANSFER, {"item_id": "письмо", "owner_id": "чужой",
                                    "location_id": "неизвестно"},
        {"письмо": str(item)}, {str(item)}, None, None, [],
    )
    assert result is None


def test_f45_rejects_reasoning_tail_inline_without_rejecting_english_world_text():
    leaked = ('За окнами проходит утренний ветер. Иара? No. Need Russian no typo. '
              'End situation meaningful: he waits. Maybe established said books not '
              'basis. We can say "Чем могу помочь?" not established but allowed. Better.')
    assert leaked_meta_lines(leaked)
    assert leaked_meta_lines("На двери табличка 'No entry'.") == []


@pytest.mark.asyncio
async def test_f39_replay_cannot_leave_player_outside_active_scene(db_session):
    campaign_id = uuid4()
    campaigns = CampaignRepository(db_session)
    locations = LocationRepository(db_session)
    entities = EntityRepository(db_session)
    scenes = SceneRepository(db_session)
    await campaigns.create(campaign_id, CampaignCreate(name="QA-216 replay"))
    origin = await locations.create(campaign_id, LocationCreate(canonical_name="Мастерская"))
    destination = await locations.create(campaign_id, LocationCreate(canonical_name="Контора"))
    hero = await entities.create_character(
        campaign_id, CharacterCreate(canonical_name="Вера", current_location_id=destination.id)
    )
    await campaigns.update(campaign_id, CampaignUpdate(player_character_id=hero.id))
    source_scene = await scenes.create(campaign_id, SceneCreate(
        title="Мастерская", location_id=origin.id,
    ))
    await db_session.flush()
    from app.services.scene_lifecycle import SceneLifecycleService
    await SceneLifecycleService(db_session).activate(campaign_id, source_scene.id)
    # Reproduce the QA-215 state: scene already restored to workshop but replay
    # leaves Character at the previous destination.
    with patch("app.services.turn_undo_service.ActiveCanonReplayService.replay",
               new_callable=AsyncMock), patch(
        "app.services.turn_undo_service.WorldReducer.rebuild", new_callable=AsyncMock
    ), patch.object(TurnUndoService, "_remove_turn_introductions", new_callable=AsyncMock):
        await TurnUndoService(db_session)._reconcile_derived_state(campaign_id, uuid4())
    player = await db_session.get(Character, str(hero.id))
    assert player.current_location_id == str(origin.id)
