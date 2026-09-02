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
from app.models.entity import EntityStatus
from app.models.location import LocationCreate
from app.models.provider_config import ProviderConfigCreate
from app.models.scene import SceneCreate
from app.services.entity_registrar import EntityRegistrar
from app.services.presence_service import PresenceService
from app.services.scene_lifecycle import SceneLifecycleService


async def _campaign_state(db_session: AsyncSession):
    campaign_id = uuid4()
    campaigns = CampaignRepository(db_session)
    entities = EntityRepository(db_session)
    locations = LocationRepository(db_session)
    scenes = SceneRepository(db_session)

    await campaigns.create(campaign_id, CampaignCreate(name="Round 42 live materialization"))
    await ProviderConfigRepository(db_session).create_or_update(
        campaign_id,
        ProviderConfigCreate(
            base_url="http://localhost:11434/v1",
            model_name="test-control",
        ),
    )
    location = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Старая гавань"),
    )
    hero = await entities.create_character(
        campaign_id,
        CharacterCreate(
            canonical_name="Рэт",
            current_location_id=location.id,
        ),
    )
    await campaigns.update(
        campaign_id,
        CampaignUpdate(player_character_id=hero.id),
    )
    scene = await scenes.create(
        campaign_id,
        SceneCreate(title="Склад у гавани", location_id=location.id),
    )
    await scenes.add_participant(scene.id, hero.id)
    await SceneLifecycleService(db_session).activate(campaign_id, scene.id)
    return campaign_id, location, hero, scene


@pytest.mark.asyncio
async def test_dead_character_cannot_be_rematerialized_by_narrator_extraction(
    db_session: AsyncSession,
):
    campaign_id, _, _, scene = await _campaign_state(db_session)
    entities = EntityRepository(db_session)
    lydia = await entities.create_character(
        campaign_id,
        CharacterCreate(
            canonical_name="Лидия",
            status=EntityStatus.DEAD,
        ),
    )
    narrator_text = "Лидия входит в склад и спокойно закрывает за собой дверь."

    with patch(
        "app.services.entity_registrar.RoleModelRouter.generate_json",
        new_callable=AsyncMock,
        return_value={
            "characters": [
                {
                    "canonical_name": "Лидия",
                    "role": "связная",
                    "evidence": narrator_text,
                    "presence": "present",
                    "importance": "major",
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

    lydia_after = await entities.get_character(lydia.id)
    scene_after = await SceneRepository(db_session).get_by_id(scene.id)

    assert lydia_after.status == "dead"
    assert lydia_after.current_location_id is None
    assert lydia.id not in scene_after.participants
    assert result.created_ids == []
    assert result.present_ids == []
    assert len(result.conflicts) == 1
    assert "cannot be materialized" in result.conflicts[0]["error"]


@pytest.mark.asyncio
async def test_presence_service_rejects_dead_character_even_with_explicit_movement(
    db_session: AsyncSession,
):
    campaign_id, _, _, scene = await _campaign_state(db_session)
    entities = EntityRepository(db_session)
    lydia = await entities.create_character(
        campaign_id,
        CharacterCreate(
            canonical_name="Лидия",
            status=EntityStatus.DEAD,
        ),
    )

    with pytest.raises(ValueError, match="cannot participate in a live scene"):
        await PresenceService(db_session).add_participant(
            scene.id,
            lydia.id,
            allow_movement=True,
        )


@pytest.mark.asyncio
async def test_named_reveal_promotes_temporary_identity_instead_of_creating_duplicate(
    db_session: AsyncSession,
):
    campaign_id, location, _, scene = await _campaign_state(db_session)
    entities = EntityRepository(db_session)
    dealer = await entities.create_character(
        campaign_id,
        CharacterCreate(
            canonical_name="Местный делец",
            current_location_id=location.id,
            custom_fields={
                "temporary_name": True,
                "role": "делец",
                "source": "entity_registrar",
            },
        ),
    )
    await SceneRepository(db_session).add_participant(scene.id, dealer.id)
    narrator_text = "Мартин Вэнс усмехается и протягивает тебе ключ от склада."

    with patch(
        "app.services.entity_registrar.RoleModelRouter.generate_json",
        new_callable=AsyncMock,
        return_value={
            "characters": [
                {
                    "canonical_name": "Мартин Вэнс",
                    "aliases": ["Вэнс"],
                    "role": "делец",
                    "evidence": "Мартин Вэнс усмехается",
                    "presence": "present",
                    "importance": "supporting",
                    "temporary_name": False,
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

    characters = await entities.list_by_campaign(campaign_id, entity_type="character")
    promoted = await entities.get_character(dealer.id)

    assert result.created_ids == []
    assert dealer.id in result.resolved_ids
    assert len(characters) == 2
    assert promoted.canonical_name == "Мартин Вэнс"
    assert "Местный делец" in promoted.aliases
    assert "Вэнс" in promoted.aliases
    assert promoted.custom_fields["temporary_name"] is False
    assert promoted.custom_fields["identity_promoted_from"] == "Местный делец"


@pytest.mark.asyncio
async def test_registrar_rejects_invented_canonical_name_not_present_in_narration(
    db_session: AsyncSession,
):
    campaign_id, _, _, scene = await _campaign_state(db_session)
    narrator_text = "Высокий чиновник у двери усмехается и велит охране отойти."

    with patch(
        "app.services.entity_registrar.RoleModelRouter.generate_json",
        new_callable=AsyncMock,
        return_value={
            "characters": [
                {
                    "canonical_name": "Городской Диктатор",
                    "role": "чиновник",
                    "evidence": narrator_text,
                    "presence": "present",
                    "importance": "supporting",
                    "temporary_name": False,
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

    characters = await EntityRepository(db_session).list_by_campaign(
        campaign_id,
        entity_type="character",
    )
    assert [item.canonical_name for item in characters] == ["Рэт"]
    assert result.created_ids == []
    assert result.resolved_ids == []


def test_registrar_rejects_synthetic_unnamed_placeholder():
    assert EntityRegistrar._clean_name("Безымянный собеседник") is None
    assert EntityRegistrar._clean_name("Неизвестный собеседник") is None
