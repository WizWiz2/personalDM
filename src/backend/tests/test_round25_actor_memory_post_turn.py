from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.job_repo import PostTurnJobRepository
from app.db.repositories.turn_repo import TurnRepository
from app.db.tables import Belief, Event, Fact
from app.models.campaign import CampaignCreate, CampaignUpdate
from app.models.character import CharacterCreate
from app.models.turn import TurnCreate
from app.runtime import install_runtime
from app.services.actor_turn_authority_guard import segment_actor_response
from app.services.post_turn_processor import PostTurnProcessor
from app.services.role_model_router import RoleModelRouter


@pytest.mark.asyncio
async def test_actor_dialogue_creates_belief_without_objective_canon(db_session):
    """Round25 live-path regression: published NPC facts must survive background Scribe."""
    install_runtime()
    campaign_id = uuid4()
    campaigns = CampaignRepository(db_session)
    entities = EntityRepository(db_session)
    turns = TurnRepository(db_session)

    await campaigns.create(campaign_id, CampaignCreate(name="Actor memory integration"))
    player = await entities.create_character(
        campaign_id,
        CharacterCreate(canonical_name="Алексей Воронцов"),
    )
    anna = await entities.create_character(
        campaign_id,
        CharacterCreate(canonical_name="Анна Левина"),
    )
    await campaigns.update(
        campaign_id,
        CampaignUpdate(player_character_id=player.id),
    )

    user = await turns.create(
        campaign_id,
        TurnCreate(
            role="user",
            content="Кто имел доступ к сейфу?",
        ),
    )
    published = (
        "Анна отвечает спокойно. «Доступ к сейфу был у трёх человек. "
        "У охраны были временные коды»."
    )
    segments = segment_actor_response(published)
    selected_ids = [
        index
        for index, value in enumerate(segments, start=1)
        if "доступ к сейфу" in value.casefold() or "временные коды" in value.casefold()
    ]
    assert len(selected_ids) >= 2

    assistant = await turns.create(
        campaign_id,
        TurnCreate(
            role="assistant",
            content=published,
            acting_character_id=anna.id,
            parent_turn_id=user.id,
            context_snapshot={
                "turn_authority": {
                    "acting_character_id": str(anna.id),
                    "acting_character": "Анна Левина",
                    "scene_disposition": "actor_turn",
                },
                "interagent_protocol": {"version": 2},
            },
        ),
    )

    processor = PostTurnProcessor(db_session)
    await processor.enqueue(campaign_id, assistant.id)
    await db_session.commit()
    jobs = await PostTurnJobRepository(db_session).list_for_turn(assistant.id)
    memory_job = next(job for job in jobs if job.job_type == "memory_scribe")

    with patch.object(
        RoleModelRouter,
        "resolve",
        new_callable=AsyncMock,
        return_value=SimpleNamespace(config=SimpleNamespace(model_name="qwen2.5:7b")),
    ), patch.object(
        RoleModelRouter,
        "generate_json",
        new_callable=AsyncMock,
        return_value={"segment_ids": selected_ids},
    ):
        await processor.process_job(memory_job.id)

    beliefs = (
        await db_session.execute(
            select(Belief).where(Belief.character_id == str(player.id))
        )
    ).scalars().all()
    assert len(beliefs) >= 2
    assert all(row.source_character_id == str(anna.id) for row in beliefs)
    assert all(row.source_turn_id == str(assistant.id) for row in beliefs)
    propositions = [row.proposition for row in beliefs]
    assert any("доступ к сейфу" in value.casefold() for value in propositions)
    assert any("временные коды" in value.casefold() for value in propositions)
    assert all(value in published for value in propositions)

    # Epistemic boundary: actor speech cannot become objective world canon through generic Scribe.
    facts = (
        await db_session.execute(select(Fact).where(Fact.campaign_id == str(campaign_id)))
    ).scalars().all()
    events = (
        await db_session.execute(select(Event).where(Event.campaign_id == str(campaign_id)))
    ).scalars().all()
    assert facts == []
    assert events == []


@pytest.mark.asyncio
async def test_actor_silence_post_turn_creates_no_belief(db_session):
    install_runtime()
    campaign_id = uuid4()
    campaigns = CampaignRepository(db_session)
    entities = EntityRepository(db_session)
    turns = TurnRepository(db_session)

    await campaigns.create(campaign_id, CampaignCreate(name="Actor silence"))
    player = await entities.create_character(
        campaign_id,
        CharacterCreate(canonical_name="Мария"),
    )
    actor = await entities.create_character(
        campaign_id,
        CharacterCreate(canonical_name="Бармен"),
    )
    await campaigns.update(campaign_id, CampaignUpdate(player_character_id=player.id))
    user = await turns.create(
        campaign_id,
        TurnCreate(role="user", content="Что вы знаете?"),
    )
    assistant = await turns.create(
        campaign_id,
        TurnCreate(
            role="assistant",
            content="Бармен умолкает.",
            acting_character_id=actor.id,
            parent_turn_id=user.id,
            context_snapshot={
                "turn_authority": {
                    "acting_character_id": str(actor.id),
                    "scene_disposition": "actor_turn",
                },
                "interagent_protocol": {"version": 2},
            },
        ),
    )
    processor = PostTurnProcessor(db_session)
    await processor.enqueue(campaign_id, assistant.id)
    await db_session.commit()
    memory_job = next(
        job
        for job in await PostTurnJobRepository(db_session).list_for_turn(assistant.id)
        if job.job_type == "memory_scribe"
    )

    generate = AsyncMock(return_value={"segment_ids": [1]})
    with patch.object(
        RoleModelRouter,
        "resolve",
        new_callable=AsyncMock,
        return_value=SimpleNamespace(config=SimpleNamespace(model_name="qwen2.5:7b")),
    ), patch.object(RoleModelRouter, "generate_json", generate):
        await processor.process_job(memory_job.id)

    beliefs = (
        await db_session.execute(
            select(Belief).where(Belief.character_id == str(player.id))
        )
    ).scalars().all()
    assert beliefs == []
    assert generate.await_count == 0
