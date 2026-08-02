from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.fact_repo import FactRepository
from app.db.repositories.narrative_detail_repo import NarrativeDetailRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.tables import Turn
from app.models.campaign import CampaignCreate
from app.models.character import CharacterCreate
from app.models.fact import FactCreate
from app.models.memory_taxonomy import NarrativeDetailCreate, NarrativeDetailType
from app.models.proposed_change import ChangeType, ProposedChangeCreate
from app.models.scene import SceneCreate
from app.services.canon_applier import CanonApplier
from app.services.canon_semantics import proposals_from_envelope
from app.services.context_compiler import ContextCompiler
from app.services.memory_taxonomy import MemoryTaxonomyService


async def campaign_scene_character(db_session: AsyncSession):
    campaign_id = uuid4()
    await CampaignRepository(db_session).create(
        campaign_id,
        CampaignCreate(name="Memory taxonomy"),
    )
    scene = await SceneRepository(db_session).create(
        campaign_id,
        SceneCreate(title="Комната над трактиром"),
    )
    character = await EntityRepository(db_session).create_character(
        campaign_id,
        CharacterCreate(canonical_name="София", aliases=["Софи"]),
    )
    await SceneRepository(db_session).add_participant(scene.id, character.id)
    return campaign_id, scene, character


def fact_proposal(
    subject: str,
    predicate: str,
    object_value: str,
    *,
    scope: str = "campaign",
    evidence: str | None = None,
) -> ProposedChangeCreate:
    return ProposedChangeCreate(
        change_type=ChangeType.FACT,
        payload={
            "subject": subject,
            "predicate": predicate,
            "object_value": object_value,
            "scope": scope,
            "visibility": "public",
            "_canon": {
                "outcome_id": "o1",
                "kind": "world_state",
                "description": evidence or f"{subject} {predicate} {object_value}",
                "evidence": evidence or f"{subject} {predicate} {object_value}",
                "authority": "public_observation",
                "operation": "assert",
                "cardinality": "single",
            },
        },
    )


@pytest.mark.asyncio
async def test_gaze_is_demoted_from_fact_to_narrative_detail(
    db_session: AsyncSession,
):
    campaign_id, scene, character = await campaign_scene_character(db_session)
    proposals = await MemoryTaxonomyService(db_session).classify_batch(
        campaign_id,
        scene.id,
        [
            fact_proposal(
                "София",
                "отвела взгляд",
                "к закрытому окну",
                evidence="София отвела взгляд к закрытому окну.",
            )
        ],
    )

    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.change_type == ChangeType.NARRATIVE_DETAIL
    assert proposal.payload["detail_type"] == "gaze"
    assert proposal.payload["scene_id"] == str(scene.id)
    assert proposal.payload["subject_entity_id"] == str(character.id)
    assert proposal.payload["_memory"]["demoted_from"] == "fact"


@pytest.mark.asyncio
async def test_persistent_wound_becomes_entity_state(
    db_session: AsyncSession,
):
    campaign_id, scene, character = await campaign_scene_character(db_session)
    proposals = await MemoryTaxonomyService(db_session).classify_batch(
        campaign_id,
        scene.id,
        [
            fact_proposal(
                "София",
                "ранена",
                "в левое плечо",
                scope="scene",
            )
        ],
    )

    proposal = proposals[0]
    assert proposal.change_type == ChangeType.FACT
    assert proposal.payload["memory_kind"] == "entity_state"
    assert proposal.payload["scope"] == "campaign"
    assert "scene_id" not in proposal.payload
    assert proposal.payload["subject_entity_id"] == str(character.id)


@pytest.mark.asyncio
async def test_open_door_remains_scene_state(db_session: AsyncSession):
    campaign_id, scene, _ = await campaign_scene_character(db_session)
    proposals = await MemoryTaxonomyService(db_session).classify_batch(
        campaign_id,
        scene.id,
        [
            fact_proposal(
                "Дверь спальни",
                "открыта",
                "настежь",
                scope="scene",
            )
        ],
    )

    proposal = proposals[0]
    assert proposal.change_type == ChangeType.FACT
    assert proposal.payload["memory_kind"] == "scene_state"
    assert proposal.payload["scope"] == "scene"
    assert proposal.payload["scene_id"] == str(scene.id)


@pytest.mark.asyncio
async def test_world_lore_remains_world_canon(db_session: AsyncSession):
    campaign_id, scene, _ = await campaign_scene_character(db_session)
    proposals = await MemoryTaxonomyService(db_session).classify_batch(
        campaign_id,
        scene.id,
        [
            fact_proposal(
                "Белая Башня",
                "является",
                "древней тюрьмой магов",
            )
        ],
    )

    proposal = proposals[0]
    assert proposal.payload["memory_kind"] == "world_canon"
    assert proposal.payload["scope"] == "campaign"


def test_non_durable_outcome_can_only_create_narrative_detail():
    text = "София на миг отвела взгляд к окну."
    proposals, audit = proposals_from_envelope(
        {
            "outcomes": [
                {
                    "id": "d1",
                    "kind": "narrative_detail",
                    "description": "Короткий взгляд к окну",
                    "evidence": text,
                    "authority": "public_observation",
                    "durable": False,
                }
            ],
            "proposals": [
                {
                    "outcome_id": "d1",
                    "change_type": "narrative_detail",
                    "payload": {
                        "text": text,
                        "detail_type": "gaze",
                    },
                }
            ],
        },
        text,
    )

    assert audit.envelope_valid is True
    assert audit.gap_count == 0
    assert proposals[0].change_type == ChangeType.NARRATIVE_DETAIL
    assert proposals[0].payload["_canon"]["durable"] is False


@pytest.mark.asyncio
async def test_narrative_detail_never_creates_fact(db_session: AsyncSession):
    campaign_id, scene, character = await campaign_scene_character(db_session)
    source_turn_id = uuid4()
    db_session.add(
        Turn(
            id=str(source_turn_id),
            campaign_id=str(campaign_id),
            scene_id=str(scene.id),
            role="assistant",
            content="София едва заметно улыбнулась.",
            status="active",
        )
    )
    await db_session.flush()

    await CanonApplier(db_session).apply(
        campaign_id,
        ChangeType.NARRATIVE_DETAIL,
        {
            "scene_id": str(scene.id),
            "text": "София едва заметно улыбнулась.",
            "detail_type": "expression",
            "subject_entity_id": str(character.id),
            "visibility": "public",
            "turn_window": 3,
        },
        source_turn_id,
    )

    assert await FactRepository(db_session).list_active(campaign_id) == []
    details = await NarrativeDetailRepository(db_session).list_by_scene(
        campaign_id,
        scene.id,
    )
    assert len(details) == 1
    assert details[0].detail_type == NarrativeDetailType.EXPRESSION


@pytest.mark.asyncio
async def test_context_includes_only_recent_details_from_current_scene(
    db_session: AsyncSession,
):
    campaign_id, scene, _ = await campaign_scene_character(db_session)
    other_scene = await SceneRepository(db_session).create(
        campaign_id,
        SceneCreate(title="Общий зал"),
    )
    base_time = datetime.utcnow() - timedelta(minutes=10)
    turn_ids: list[UUID] = []
    for index in range(4):
        turn_id = uuid4()
        turn_ids.append(turn_id)
        db_session.add(
            Turn(
                id=str(turn_id),
                campaign_id=str(campaign_id),
                scene_id=str(scene.id),
                role="assistant",
                content=f"Нейтральный ответ {index}",
                status="active",
                created_at=base_time + timedelta(minutes=index),
            )
        )
    other_turn_id = uuid4()
    db_session.add(
        Turn(
            id=str(other_turn_id),
            campaign_id=str(campaign_id),
            scene_id=str(other_scene.id),
            role="assistant",
            content="Ответ в другой сцене",
            status="active",
            created_at=base_time + timedelta(minutes=5),
        )
    )
    await db_session.flush()

    details = NarrativeDetailRepository(db_session)
    await details.create(
        campaign_id,
        NarrativeDetailCreate(
            scene_id=scene.id,
            source_turn_id=turn_ids[0],
            text="СТАРЫЙ ВЗГЛЯД НЕ ДОЛЖЕН ВЕРНУТЬСЯ",
            detail_type=NarrativeDetailType.GAZE,
        ),
    )
    await details.create(
        campaign_id,
        NarrativeDetailCreate(
            scene_id=scene.id,
            source_turn_id=turn_ids[-1],
            text="На подоконнике ещё дрожит полоска лунного света.",
            detail_type=NarrativeDetailType.AMBIENT,
        ),
    )
    await details.create(
        campaign_id,
        NarrativeDetailCreate(
            scene_id=other_scene.id,
            source_turn_id=other_turn_id,
            text="Шум общего зала остаётся внизу.",
            detail_type=NarrativeDetailType.AMBIENT,
        ),
    )
    await db_session.commit()

    messages, metadata = await ContextCompiler(db_session).compile_context(
        campaign_id=campaign_id,
        scene_id=scene.id,
    )
    context = "\n".join(message.content for message in messages)
    assert "полоска лунного света" in context
    assert "СТАРЫЙ ВЗГЛЯД" not in context
    assert "Шум общего зала" not in context
    assert metadata["included_narrative_detail_ids"]
    assert "layer_1b_recent_narrative_details" in metadata["included_layers"]


@pytest.mark.asyncio
async def test_fact_versions_do_not_cross_memory_kinds(db_session: AsyncSession):
    campaign_id, scene, _ = await campaign_scene_character(db_session)
    facts = FactRepository(db_session)
    world = await facts.apply_change(
        campaign_id,
        FactCreate(
            subject="Башня",
            predicate="состояние",
            object_value="древняя",
            memory_kind="world_canon",
        ),
    )
    scene_fact = await facts.apply_change(
        campaign_id,
        FactCreate(
            subject="Башня",
            predicate="состояние",
            object_value="в дыму",
            scope="scene",
            scene_id=scene.id,
            memory_kind="scene_state",
        ),
    )

    active = await facts.list_active(campaign_id, scene_id=scene.id)
    assert {fact.id for fact in active} == {world.id, scene_fact.id}
    assert {fact.memory_kind for fact in active} == {"world_canon", "scene_state"}
