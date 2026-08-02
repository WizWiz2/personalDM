from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.location_repo import LocationRepository
from app.db.repositories.provider_config_repo import ProviderConfigRepository
from app.db.repositories.scene_repo import SceneRepository
from app.models.campaign import CampaignCreate, CampaignUpdate
from app.models.character import CharacterCreate
from app.models.location import LocationCreate
from app.models.provider_config import ProviderConfigCreate
from app.models.scene import SceneCreate
from app.services.entity_registrar import EntityRegistrar
from app.services.scene_lifecycle import SceneLifecycleService


async def _campaign_state(db_session: AsyncSession):
    campaign_id = uuid4()
    campaigns = CampaignRepository(db_session)
    entities = EntityRepository(db_session)
    locations = LocationRepository(db_session)
    scenes = SceneRepository(db_session)

    await campaigns.create(campaign_id, CampaignCreate(name="NPC registration"))
    await ProviderConfigRepository(db_session).create_or_update(
        campaign_id,
        ProviderConfigCreate(
            base_url="http://localhost:11434/v1",
            model_name="test-control",
        ),
    )
    tavern = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Таверна «Медный Котёл»"),
    )
    guild = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Гильдия купцов"),
    )
    hero = await entities.create_character(
        campaign_id,
        CharacterCreate(
            canonical_name="Эйдан",
            current_location_id=tavern.id,
        ),
    )
    await campaigns.update(
        campaign_id,
        CampaignUpdate(player_character_id=hero.id),
    )
    scene = await scenes.create(
        campaign_id,
        SceneCreate(title="Вечер в таверне", location_id=tavern.id),
    )
    await scenes.add_participant(scene.id, hero.id)
    await SceneLifecycleService(db_session).activate(campaign_id, scene.id)
    return campaign_id, tavern, guild, hero, scene


@pytest.mark.asyncio
async def test_registrar_creates_present_npc_once_and_assigns_scene_location(
    db_session: AsyncSession,
):
    campaign_id, tavern, _, hero, scene = await _campaign_state(db_session)
    narrator_text = (
        "Бармен Роэн ставит перед тобой кружку и говорит: «Комната наверху свободна»."
    )
    envelope = {
        "characters": [
            {
                "canonical_name": "Бармен Роэн",
                "aliases": ["Роэн"],
                "description": "Бармен Медного Котла.",
                "appearance": "Седая борода и закатанные рукава.",
                "role": "бармен",
                "evidence": "Бармен Роэн ставит перед тобой кружку",
                "presence": "present",
                "importance": "supporting",
                "temporary_name": False,
                "persistent": True,
            }
        ]
    }

    with patch(
        "app.services.entity_registrar.RoleModelRouter.generate_json",
        new_callable=AsyncMock,
        return_value=envelope,
    ):
        first = await EntityRegistrar(db_session).register_from_turn(
            campaign_id,
            scene.id,
            uuid4(),
            narrator_text,
        )
        second = await EntityRegistrar(db_session).register_from_turn(
            campaign_id,
            scene.id,
            uuid4(),
            narrator_text,
        )

    characters = await EntityRepository(db_session).list_by_campaign(
        campaign_id,
        entity_type="character",
    )
    assert sorted(character.canonical_name for character in characters) == [
        "Бармен Роэн",
        "Эйдан",
    ]
    bartender = next(
        character for character in characters if character.canonical_name == "Бармен Роэн"
    )
    card = await EntityRepository(db_session).get_character(bartender.id)
    assert bartender.provenance == "narrator_extracted"
    assert card.current_location_id == tavern.id
    assert bartender.id in (await SceneRepository(db_session).get_by_id(scene.id)).participants
    assert hero.id in (await SceneRepository(db_session).get_by_id(scene.id)).participants
    assert first.created_ids == [bartender.id]
    assert second.created_ids == []
    assert second.conflicts == []


@pytest.mark.asyncio
async def test_registrar_does_not_teleport_known_npc_from_another_location(
    db_session: AsyncSession,
):
    campaign_id, _, guild, _, scene = await _campaign_state(db_session)
    entities = EntityRepository(db_session)
    remote = await entities.create_character(
        campaign_id,
        CharacterCreate(
            canonical_name="Купец Тарек",
            current_location_id=guild.id,
        ),
    )
    narrator_text = "Купец Тарек входит в таверну и садится рядом с тобой."

    with patch(
        "app.services.entity_registrar.RoleModelRouter.generate_json",
        new_callable=AsyncMock,
        return_value={
            "characters": [
                {
                    "canonical_name": "Купец Тарек",
                    "evidence": narrator_text,
                    "presence": "present",
                    "importance": "supporting",
                }
            ]
        },
    ):
        result = await EntityRegistrar(db_session).register_from_turn(
            campaign_id,
            scene.id,
            uuid4(),
            narrator_text,
        )

    remote_after = await entities.get_character(remote.id)
    scene_after = await SceneRepository(db_session).get_by_id(scene.id)
    assert remote_after.current_location_id == guild.id
    assert remote.id not in scene_after.participants
    assert len(result.conflicts) == 1
    assert "without an explicit structured movement" in result.conflicts[0]["error"]
    gaps = result.gap_proposals(scene.id)
    assert len(gaps) == 1
    assert gaps[0].change_type.value == "canon_gap"


@pytest.mark.asyncio
async def test_scene_participant_insertion_requires_explicit_movement(
    db_session: AsyncSession,
):
    campaign_id, tavern, guild, _, scene = await _campaign_state(db_session)
    entities = EntityRepository(db_session)
    remote = await entities.create_character(
        campaign_id,
        CharacterCreate(
            canonical_name="Посыльный",
            current_location_id=guild.id,
        ),
    )
    scenes = SceneRepository(db_session)

    with pytest.raises(ValueError, match="explicit structured movement"):
        await scenes.add_participant(scene.id, remote.id)

    await scenes.add_participant(scene.id, remote.id, allow_movement=True)
    moved = await entities.get_character(remote.id)
    assert moved.current_location_id == tavern.id
    assert remote.id in (await scenes.get_by_id(scene.id)).participants
