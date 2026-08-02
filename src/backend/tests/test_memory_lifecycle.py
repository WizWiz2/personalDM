from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.narrative_detail_table import NarrativeDetail
from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.fact_memory_repo import FactMemoryRepository
from app.db.repositories.fact_repo import FactRepository
from app.db.repositories.location_repo import LocationRepository
from app.db.repositories.narrative_detail_repo import NarrativeDetailRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.tables import Turn
from app.models.campaign import CampaignCreate, CampaignUpdate
from app.models.character import CharacterCreate
from app.models.fact import FactCreate
from app.models.location import LocationCreate
from app.models.memory_semantics import MemoryClass, NarrativeDetailCreate
from app.models.proposed_change import ChangeType, ProposedChangeCreate
from app.models.scene import SceneCreate
from app.services.canon_semantics import proposals_from_envelope
from app.services.context_compiler import ContextCompiler
from app.services.continuity_checker import ContinuityChecker
from app.services.scene_lifecycle import SceneLifecycleService


@pytest.mark.parametrize(
    ("change_type", "payload", "expected_class"),
    [
        (
            "movement",
            {
                "character_id": "Герой",
                "location_id": "Архив",
                "description": "Герой вошёл в архив.",
            },
            "entity_state",
        ),
        (
            "fact",
            {
                "subject": "каменная дверь",
                "predicate": "состояние",
                "object_value": "открыта",
                "scope": "scene",
            },
            "scene_state",
        ),
        (
            "event",
            {
                "event_type": "discovery",
                "description": "Группа нашла архив.",
            },
            "world_canon",
        ),
    ],
)
def test_legacy_envelope_infers_memory_class(
    change_type: str,
    payload: dict,
    expected_class: str,
):
    evidence = "Герой вошёл в архив и каменная дверь открылась. Группа нашла архив."
    proposals, audit = proposals_from_envelope(
        {
            "outcomes": [
                {
                    "id": "o1",
                    "kind": "event" if change_type == "event" else "world_state",
                    "description": "Подтверждённое последствие.",
                    "evidence": evidence,
                    "authority": "dm_confirmed",
                    "durable": True,
                }
            ],
            "proposals": [
                {
                    "outcome_id": "o1",
                    "change_type": change_type,
                    "payload": payload,
                }
            ],
        },
        evidence,
    )

    assert audit.envelope_valid is True
    assert audit.inferred_memory_count == 1
    assert len(proposals) == 1
    assert proposals[0].payload["_memory"]["class"] == expected_class


def test_explicit_narrative_detail_is_transient_not_a_canon_gap():
    text = "Дождь ровно стучит по узкому подоконнику."
    proposals, audit = proposals_from_envelope(
        {
            "outcomes": [
                {
                    "id": "detail",
                    "kind": "narrative_detail",
                    "description": "Слышен ритм дождя.",
                    "evidence": text,
                    "authority": "public_observation",
                    "durable": False,
                    "memory_class": "narrative_detail",
                    "retention": "recent_turns",
                    "ttl_turns": 2,
                }
            ],
            "proposals": [
                {
                    "outcome_id": "detail",
                    "change_type": "narrative_detail",
                    "payload": {
                        "detail_type": "sensory",
                        "text": text,
                        "participant_ids": [],
                        "salience": 0.6,
                        "ttl_turns": 2,
                    },
                }
            ],
        },
        text,
    )

    assert audit.envelope_valid is True
    assert audit.gap_count == 0
    assert audit.detail_count == 1
    assert proposals[0].change_type == ChangeType.NARRATIVE_DETAIL
    assert proposals[0].payload["_memory"] == {
        "class": "narrative_detail",
        "retention": "recent_turns",
        "ttl_turns": 2,
    }


async def _setup_two_scene_campaign(db_session: AsyncSession):
    campaign_id = uuid4()
    campaigns = CampaignRepository(db_session)
    entities = EntityRepository(db_session)
    locations = LocationRepository(db_session)
    scenes = SceneRepository(db_session)

    await campaigns.create(campaign_id, CampaignCreate(name="Memory lifecycle"))
    hall = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Общий зал"),
    )
    room = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Комната"),
    )
    hero = await entities.create_character(
        campaign_id,
        CharacterCreate(canonical_name="Эйдан"),
    )
    bartender = await entities.create_character(
        campaign_id,
        CharacterCreate(canonical_name="Бармен"),
    )
    await campaigns.update(
        campaign_id,
        CampaignUpdate(player_character_id=hero.id),
    )
    first = await scenes.create(
        campaign_id,
        SceneCreate(title="Общий зал", location_id=hall.id),
    )
    second = await scenes.create(
        campaign_id,
        SceneCreate(title="Комната", location_id=room.id),
    )
    await SceneLifecycleService(db_session).activate(campaign_id, first.id)
    return campaign_id, hero, bartender, first, second


@pytest.mark.asyncio
async def test_context_separates_memory_layers_and_expires_old_scene_details(
    db_session: AsyncSession,
):
    campaign_id, hero, bartender, first, second = await _setup_two_scene_campaign(
        db_session
    )
    facts = FactRepository(db_session)
    memory_links = FactMemoryRepository(db_session)

    world = await facts.create(
        campaign_id,
        FactCreate(
            subject="Сталицк",
            predicate="расположен",
            object_value="у северной реки",
            visibility="public",
            scope="campaign",
        ),
    )
    await memory_links.assign(world.id, MemoryClass.WORLD_CANON)

    hero_state = await facts.create(
        campaign_id,
        FactCreate(
            subject="Эйдан",
            predicate="одежда",
            object_value="дорожный плащ",
            visibility="public",
            scope="campaign",
        ),
    )
    await memory_links.assign(
        hero_state.id,
        MemoryClass.ENTITY_STATE,
        hero.id,
    )

    absent_state = await facts.create(
        campaign_id,
        FactCreate(
            subject="Бармен",
            predicate="держит",
            object_value="медную кружку",
            visibility="public",
            scope="campaign",
        ),
    )
    await memory_links.assign(
        absent_state.id,
        MemoryClass.ENTITY_STATE,
        bartender.id,
    )

    scene_fact = await facts.create(
        campaign_id,
        FactCreate(
            subject="дверь общего зала",
            predicate="состояние",
            object_value="закрыта",
            visibility="public",
            scope="scene",
            scene_id=first.id,
        ),
    )
    await memory_links.assign(scene_fact.id, MemoryClass.SCENE_STATE)

    source_turn = Turn(
        campaign_id=str(campaign_id),
        scene_id=str(first.id),
        role="assistant",
        content="Дождь стучит по окну.",
        status="active",
        created_at=datetime.now(UTC),
    )
    db_session.add(source_turn)
    await db_session.flush()
    detail = await NarrativeDetailRepository(db_session).capture(
        campaign_id,
        first.id,
        UUID(source_turn.id),
        NarrativeDetailCreate(
            detail_type="sensory",
            text="Дождь стучит по окну.",
            participant_ids=[hero.id],
            ttl_turns=3,
        ),
    )

    messages, metadata = await ContextCompiler(db_session).compile_context(
        campaign_id=campaign_id,
        scene_id=first.id,
    )
    prompt = messages[0].content
    assert "[World Canon]" in prompt
    assert "Сталицк расположен у северной реки" in prompt
    assert "[Relevant Entity State]" in prompt
    assert "Эйдан одежда дорожный плащ" in prompt
    assert "Бармен держит медную кружку" not in prompt
    assert "[Current Scene State Memory]" in prompt
    assert "дверь общего зала состояние закрыта" in prompt
    assert "[Recent Narrative Details — transient, not canon]" in prompt
    assert "Дождь стучит по окну." in prompt
    assert str(detail.id) in metadata["included_narrative_detail_ids"]

    await SceneLifecycleService(db_session).activate(campaign_id, second.id)
    messages, metadata = await ContextCompiler(db_session).compile_context(
        campaign_id=campaign_id,
        scene_id=second.id,
    )
    prompt = messages[0].content
    assert "Сталицк расположен у северной реки" in prompt
    assert "Эйдан одежда дорожный плащ" in prompt
    assert "дверь общего зала состояние закрыта" not in prompt
    assert "Дождь стучит по окну." not in prompt
    assert metadata["included_narrative_detail_ids"] == []

    stored = await db_session.get(NarrativeDetail, str(detail.id))
    assert stored is not None
    assert stored.status == "expired"


@pytest.mark.asyncio
async def test_narrative_detail_is_idempotent_and_expires_after_ttl(
    db_session: AsyncSession,
):
    campaign_id, hero, _, first, _ = await _setup_two_scene_campaign(db_session)
    base = datetime.now(UTC)
    first_turn = Turn(
        campaign_id=str(campaign_id),
        scene_id=str(first.id),
        role="assistant",
        content="Первый ответ.",
        status="active",
        created_at=base,
    )
    db_session.add(first_turn)
    await db_session.flush()

    repository = NarrativeDetailRepository(db_session)
    data = NarrativeDetailCreate(
        detail_type="gesture",
        text="Эйдан держит ладонь на дверной ручке.",
        participant_ids=[hero.id],
        ttl_turns=2,
    )
    captured = await repository.capture(
        campaign_id,
        first.id,
        UUID(first_turn.id),
        data,
    )
    duplicate = await repository.capture(
        campaign_id,
        first.id,
        UUID(first_turn.id),
        data,
    )
    assert duplicate.id == captured.id

    second_turn = Turn(
        campaign_id=str(campaign_id),
        scene_id=str(first.id),
        role="assistant",
        content="Второй ответ.",
        status="active",
        created_at=base + timedelta(seconds=1),
    )
    db_session.add(second_turn)
    await db_session.flush()
    recent = await repository.list_recent(campaign_id, first.id)
    assert [item.id for item in recent] == [captured.id]

    third_turn = Turn(
        campaign_id=str(campaign_id),
        scene_id=str(first.id),
        role="assistant",
        content="Третий ответ.",
        status="active",
        created_at=base + timedelta(seconds=2),
    )
    db_session.add(third_turn)
    await db_session.flush()
    assert await repository.list_recent(campaign_id, first.id) == []
    assert await repository.prune_scene(first.id) == 1


@pytest.mark.asyncio
async def test_continuity_checker_rejects_memory_type_mismatches(
    db_session: AsyncSession,
):
    campaign_id = uuid4()
    await CampaignRepository(db_session).create(
        campaign_id,
        CampaignCreate(name="Memory validation"),
    )
    checker = ContinuityChecker(db_session)

    valid, warning = await checker.validate_change(
        campaign_id,
        ProposedChangeCreate(
            change_type=ChangeType.EVENT,
            payload={
                "event_type": "arrival",
                "description": "Караван прибыл.",
                "_memory": {
                    "class": "entity_state",
                    "retention": "until_superseded",
                },
            },
        ),
    )
    assert valid is False
    assert "event cannot use memory class entity_state" in (warning or "")

    valid, warning = await checker.validate_change(
        campaign_id,
        ProposedChangeCreate(
            change_type=ChangeType.FACT,
            payload={
                "subject": "неизвестный персонаж",
                "predicate": "состояние",
                "object_value": "ранен",
                "scope": "campaign",
                "_memory": {
                    "class": "entity_state",
                    "retention": "until_superseded",
                },
            },
        ),
    )
    assert valid is False
    assert "subject_entity_id is required" in (warning or "")
