from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.location_repo import LocationRepository
from app.db.repositories.scene_repo import SceneRepository
from app.models.campaign import CampaignCreate
from app.models.character import CharacterCreate
from app.models.entity import EntityStatus
from app.models.location import LocationCreate
from app.models.scene import SceneCreate
from app.models.turn_authority import PlannedNpcIntroduction, TurnAuthority
from app.services.turn_outcome_materializer import TurnOutcomeMaterializer


async def _scene(db_session: AsyncSession):
    campaign_id = uuid4()
    await CampaignRepository(db_session).create(
        campaign_id,
        CampaignCreate(name="Round 41 identity regression"),
    )
    location = await LocationRepository(db_session).create(
        campaign_id,
        LocationCreate(canonical_name="Трактир"),
    )
    scene = await SceneRepository(db_session).create(
        campaign_id,
        SceneCreate(title="Трактир", location_id=location.id),
    )
    return campaign_id, location, scene


def _authority(campaign_id, scene_id, introduction):
    return TurnAuthority(
        campaign_id=campaign_id,
        trigger_turn_id=uuid4(),
        player_input="Продолжаю разговор.",
        target_scene_id=scene_id,
        allowed_new_npcs=[introduction],
    )


@pytest.mark.asyncio
async def test_named_identity_reuses_structured_temporary_npc_and_rollback_restores_name(
    db_session: AsyncSession,
):
    campaign_id, location, scene = await _scene(db_session)
    entities = EntityRepository(db_session)
    scenes = SceneRepository(db_session)
    temporary = await entities.create_character(
        campaign_id,
        CharacterCreate(
            canonical_name="Местный делец",
            description="Местный делец.",
            current_location_id=location.id,
            custom_fields={
                "introduced_by": "turn_authority",
                "role": "делец",
                "temporary_name": True,
            },
        ),
    )
    await scenes.add_participant(scene.id, temporary.id)

    authority = _authority(
        campaign_id,
        scene.id,
        PlannedNpcIntroduction(
            canonical_name="Мартин Вэнс",
            role="делец",
            temporary_name=False,
            reason="Собеседник наконец представился.",
        ),
    )
    materializer = TurnOutcomeMaterializer(db_session)
    outcome = await materializer.materialize(authority, source_turn_id=uuid4())

    characters = await entities.list_by_campaign(campaign_id, entity_type="character")
    assert len(characters) == 1
    assert outcome.introduced_character_ids == ()
    assert len(outcome.renamed_existing_characters) == 1

    resolved = await entities.get_by_id(temporary.id)
    assert resolved is not None
    assert resolved.canonical_name == "Мартин Вэнс"
    assert "Местный делец" in resolved.aliases
    assert resolved.custom_fields["temporary_name"] is False

    await materializer.rollback(outcome)
    restored = await entities.get_by_id(temporary.id)
    assert restored is not None
    assert restored.canonical_name == "Местный делец"
    assert restored.aliases == []
    assert restored.custom_fields["temporary_name"] is True


@pytest.mark.asyncio
async def test_dead_existing_identity_cannot_be_reintroduced_as_new_npc(
    db_session: AsyncSession,
):
    campaign_id, location, scene = await _scene(db_session)
    entities = EntityRepository(db_session)
    dead = await entities.create_character(
        campaign_id,
        CharacterCreate(
            canonical_name="Лидия",
            status=EntityStatus.DEAD,
            current_location_id=location.id,
        ),
    )

    authority = _authority(
        campaign_id,
        scene.id,
        PlannedNpcIntroduction(
            canonical_name="Лидия",
            role="тётя героя",
            reason="Имя встретилось в текущем разговоре.",
        ),
    )

    with pytest.raises(ValueError, match="resolved identity is not active"):
        await TurnOutcomeMaterializer(db_session).materialize(
            authority,
            source_turn_id=uuid4(),
        )

    characters = await entities.list_by_campaign(campaign_id, entity_type="character")
    assert [character.id for character in characters] == [dead.id]


@pytest.mark.asyncio
async def test_existing_identity_in_another_location_is_not_duplicated_or_teleported(
    db_session: AsyncSession,
):
    campaign_id, _, scene = await _scene(db_session)
    entities = EntityRepository(db_session)
    remote_location = await LocationRepository(db_session).create(
        campaign_id,
        LocationCreate(canonical_name="Нотариальная контора"),
    )
    existing = await entities.create_character(
        campaign_id,
        CharacterCreate(
            canonical_name="Мартин Вэнс",
            current_location_id=remote_location.id,
        ),
    )

    authority = _authority(
        campaign_id,
        scene.id,
        PlannedNpcIntroduction(
            canonical_name="Мартин Вэнс",
            role="делец",
            reason="Planner ошибочно пометил известного NPC как нового.",
        ),
    )

    with pytest.raises(ValueError, match="existing character in another location"):
        await TurnOutcomeMaterializer(db_session).materialize(
            authority,
            source_turn_id=uuid4(),
        )

    characters = await entities.list_by_campaign(campaign_id, entity_type="character")
    assert [character.id for character in characters] == [existing.id]
    card = await entities.get_character(existing.id)
    assert card.current_location_id == remote_location.id
