from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.memory_taxonomy_table import FactMemoryProfile, NarrativeDetail
from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.fact_repo import FactRepository
from app.db.repositories.narrative_detail_repo import NarrativeDetailRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.tables import SceneThesis, Turn
from app.db.thesis_lifecycle_table import ThesisLifecycleProfile
from app.models.campaign import CampaignCreate
from app.models.fact import FactCreate
from app.models.memory_ops import MemoryMaintenanceRequest
from app.models.memory_taxonomy import NarrativeDetailCreate, NarrativeDetailType
from app.models.scene import SceneCreate
from app.models.scene_thesis import SceneThesisCreate, ThesisType
from app.services.memory_operations import MemoryOperationsService


async def _setup(db_session: AsyncSession):
    campaign_id = uuid4()
    await CampaignRepository(db_session).create(
        campaign_id,
        CampaignCreate(name="Memory operations"),
    )
    scene = await SceneRepository(db_session).create(
        campaign_id,
        SceneCreate(title="Рабочая сцена"),
    )

    old_turn = Turn(
        campaign_id=str(campaign_id),
        scene_id=str(scene.id),
        role="assistant",
        content="Старый ответ.",
        status="active",
    )
    new_turn = Turn(
        campaign_id=str(campaign_id),
        scene_id=str(scene.id),
        role="assistant",
        content="Новый ответ.",
        status="active",
    )
    db_session.add_all([old_turn, new_turn])
    await db_session.flush()

    fact = await FactRepository(db_session).create(
        campaign_id,
        FactCreate(
            subject="Белая Башня",
            predicate="расположена",
            object_value="на севере",
            visibility="public",
            scope="campaign",
        ),
    )

    detail = await NarrativeDetailRepository(db_session).create(
        campaign_id,
        NarrativeDetailCreate(
            scene_id=scene.id,
            source_turn_id=UUID(old_turn.id),
            detail_type=NarrativeDetailType.SENSORY,
            text="Дождь стучит по стеклу.",
            turn_window=1,
        ),
    )

    repo = SceneRepository(db_session)
    keeper = await repo.create_thesis(
        scene.id,
        SceneThesisCreate(
            thesis_type=ThesisType.TENSION,
            text="Стража проверяет документы.",
            priority=8,
        ),
        source_turn_id=UUID(new_turn.id),
    )
    duplicate = await repo.create_thesis(
        scene.id,
        SceneThesisCreate(
            thesis_type=ThesisType.TENSION,
            text="Проверка документов продолжается.",
            priority=2,
        ),
        source_turn_id=UUID(old_turn.id),
    )
    stale = await repo.create_thesis(
        scene.id,
        SceneThesisCreate(
            thesis_type=ThesisType.VISUAL_STATE,
            text="На столе горит одна свеча.",
            priority=1,
        ),
        source_turn_id=UUID(old_turn.id),
    )

    db_session.add_all(
        [
            ThesisLifecycleProfile(
                thesis_id=str(keeper.id),
                semantic_key="проверка документов",
                ttl_turns=5,
                last_reinforced_turn_id=new_turn.id,
            ),
            ThesisLifecycleProfile(
                thesis_id=str(duplicate.id),
                semantic_key="проверка документов",
                ttl_turns=5,
                last_reinforced_turn_id=old_turn.id,
            ),
            ThesisLifecycleProfile(
                thesis_id=str(stale.id),
                semantic_key="одна свеча",
                ttl_turns=1,
                last_reinforced_turn_id=old_turn.id,
            ),
        ]
    )
    await db_session.commit()
    return campaign_id, fact, detail, keeper, duplicate, stale


@pytest.mark.asyncio
async def test_memory_operations_dry_run_is_non_mutating(db_session: AsyncSession):
    campaign_id, fact, detail, keeper, duplicate, stale = await _setup(db_session)
    service = MemoryOperationsService(db_session)

    snapshot = await service.snapshot(campaign_id)
    assert snapshot["health"]["memory_profile_errors"] == 1
    assert snapshot["health"]["expired_transient_details"] == 1
    assert snapshot["health"]["stale_or_duplicate_theses"] == 2

    result = await service.maintain(
        campaign_id,
        MemoryMaintenanceRequest(apply_changes=False),
    )
    assert result.applied is False
    assert {item.action for item in result.actions} == {
        "create_profile",
        "remove_expired",
        "resolve",
    }

    assert await db_session.get(FactMemoryProfile, str(fact.id)) is None
    assert await db_session.get(NarrativeDetail, str(detail.id)) is not None
    assert (await db_session.get(SceneThesis, str(keeper.id))).status == "active"
    assert (await db_session.get(SceneThesis, str(duplicate.id))).status == "active"
    assert (await db_session.get(SceneThesis, str(stale.id))).status == "active"


@pytest.mark.asyncio
async def test_memory_operations_apply_repairs_only_candidates(
    db_session: AsyncSession,
):
    campaign_id, fact, detail, keeper, duplicate, stale = await _setup(db_session)
    result = await MemoryOperationsService(db_session).maintain(
        campaign_id,
        MemoryMaintenanceRequest(apply_changes=True),
    )
    await db_session.commit()

    assert result.applied is True
    assert result.profiles_repaired == 1
    assert result.details_cleaned == 1
    assert result.theses_closed == 2
    assert await db_session.get(FactMemoryProfile, str(fact.id)) is not None
    assert await db_session.get(NarrativeDetail, str(detail.id)) is None
    assert (await db_session.get(SceneThesis, str(keeper.id))).status == "active"
    assert (await db_session.get(SceneThesis, str(duplicate.id))).status == "resolved"
    assert (await db_session.get(SceneThesis, str(stale.id))).status == "resolved"


@pytest.mark.asyncio
async def test_memory_operations_api_defaults_to_dry_run(
    client: TestClient,
    db_session: AsyncSession,
):
    campaign_id, *_ = await _setup(db_session)

    page = client.get("/api/memory-ops")
    assert page.status_code == 200
    assert "Memory Operations" in page.text

    response = client.post(
        f"/api/campaigns/{campaign_id}/memory-ops/maintenance",
        json={},
    )
    assert response.status_code == 200, response.text
    assert response.json()["applied"] is False
