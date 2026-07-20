from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.fact_repo import FactRepository
from app.db.repositories.provider_config_repo import ProviderConfigRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.repositories.turn_repo import TurnRepository
from app.models.campaign import CampaignCreate
from app.models.character import CharacterCreate
from app.models.entity import EntityCreate, EntityType
from app.models.fact import FactCreate
from app.models.provider_config import ProviderConfigCreate
from app.models.proposed_change import ChangeType
from app.models.scene import SceneCreate
from app.models.turn import TurnCreate
from app.services.context_compiler import ContextCompiler
from app.services.memory_scribe import MemoryScribe


async def configured_campaign(db_session: AsyncSession, name: str):
    campaign_id = uuid4()
    await CampaignRepository(db_session).create(campaign_id, CampaignCreate(name=name))
    await ProviderConfigRepository(db_session).create_or_update(
        campaign_id,
        ProviderConfigCreate(
            base_url="http://localhost:11434/v1",
            model_name="test",
            context_window=2200,
        ),
    )
    return campaign_id


@pytest.mark.asyncio
async def test_manifest_lists_only_facts_actually_sent(db_session: AsyncSession):
    campaign_id = await configured_campaign(db_session, "Manifest accuracy")
    actor = await EntityRepository(db_session).create_character(
        campaign_id,
        CharacterCreate(
            entity_type=EntityType.CHARACTER,
            canonical_name="Actor",
            description="A concise actor",
        ),
    )
    scene = await SceneRepository(db_session).create(
        campaign_id, SceneCreate(title="Small room")
    )
    await SceneRepository(db_session).add_participant(scene.id, actor.id)
    for index in range(12):
        await FactRepository(db_session).create(
            campaign_id,
            FactCreate(
                subject=f"Oversized subject {index}",
                predicate="contains",
                object_value="x" * 600,
                visibility="public",
            ),
        )
    await db_session.commit()

    messages, metadata = await ContextCompiler(db_session).compile_context(
        campaign_id=campaign_id,
        acting_character_id=actor.id,
        scene_id=scene.id,
        current_user_content="Continue.",
    )

    assert "[Campaign Facts & History]" not in "\n".join(
        message.content for message in messages
    )
    assert metadata["included_fact_ids"] == []


@pytest.mark.asyncio
async def test_repeated_current_message_is_always_last(db_session: AsyncSession):
    campaign_id = await configured_campaign(db_session, "Repeated input")
    actor = await EntityRepository(db_session).create_character(
        campaign_id,
        CharacterCreate(
            entity_type=EntityType.CHARACTER,
            canonical_name="Actor",
            description="y" * 2500,
        ),
    )
    scene = await SceneRepository(db_session).create(
        campaign_id, SceneCreate(title="Conversation")
    )
    await SceneRepository(db_session).add_participant(scene.id, actor.id)
    await TurnRepository(db_session).create(
        campaign_id,
        TurnCreate(
            role="user",
            content="Я молчу.",
            scene_id=scene.id,
            acting_character_id=actor.id,
        ),
    )
    await db_session.commit()

    messages, metadata = await ContextCompiler(db_session).compile_context(
        campaign_id=campaign_id,
        acting_character_id=actor.id,
        scene_id=scene.id,
        current_user_content="Я молчу.",
    )

    assert messages[-1].role == "user"
    assert messages[-1].content == "Я молчу."
    assert metadata["current_user_reserved"] is True


@pytest.mark.asyncio
async def test_knowledge_without_explicit_recipient_is_rejected(
    db_session: AsyncSession,
):
    source_id = uuid4()
    player_id = uuid4()
    normalized = MemoryScribe(db_session)._normalize_payload(
        ChangeType.KNOWLEDGE,
        {
            "source_character_id": str(source_id),
            "proposition": "The gate is open",
        },
        known_entities={},
        known_ids={str(source_id), str(player_id)},
        acting_character_id=source_id,
        player_character_id=player_id,
        scene_participant_ids=[str(source_id), str(player_id)],
    )
    assert normalized is None


@pytest.mark.asyncio
async def test_scene_rejects_cross_campaign_and_non_character_participants(
    db_session: AsyncSession,
):
    campaign_a = uuid4()
    campaign_b = uuid4()
    await CampaignRepository(db_session).create(campaign_a, CampaignCreate(name="A"))
    await CampaignRepository(db_session).create(campaign_b, CampaignCreate(name="B"))
    scene = await SceneRepository(db_session).create(
        campaign_a, SceneCreate(title="A scene")
    )
    outsider = await EntityRepository(db_session).create_character(
        campaign_b,
        CharacterCreate(
            entity_type=EntityType.CHARACTER,
            canonical_name="Outsider",
        ),
    )
    location = await EntityRepository(db_session).create(
        campaign_a,
        EntityCreate(
            entity_type=EntityType.LOCATION,
            canonical_name="Courtyard",
        ),
    )

    with pytest.raises(ValueError, match="same campaign"):
        await SceneRepository(db_session).add_participant(scene.id, outsider.id)
    with pytest.raises(ValueError, match="Only character"):
        await SceneRepository(db_session).add_participant(scene.id, location.id)
