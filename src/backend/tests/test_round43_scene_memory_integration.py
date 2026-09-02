from uuid import UUID, uuid4

import pytest

from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.tables import Turn
from app.models.campaign import CampaignCreate, CampaignUpdate
from app.models.character import CharacterCreate
from app.models.scene import SceneCreate
from app.models.scene_thesis import ThesisType
from app.services.context_compiler import ContextCompiler
from app.services.memory_operations import MemoryOperationsService
from app.services.thesis_curator import DesiredThesis, ThesisCurator


async def _assistant_turn(db_session, campaign_id, scene_id, content: str) -> UUID:
    row = Turn(
        campaign_id=str(campaign_id),
        scene_id=str(scene_id),
        role="assistant",
        content=content,
        status="active",
    )
    db_session.add(row)
    await db_session.flush()
    return UUID(row.id)


@pytest.mark.asyncio
async def test_scene_threads_npc_and_context_survive_curator_cycles(db_session):
    campaign_id = uuid4()
    campaigns = CampaignRepository(db_session)
    entities = EntityRepository(db_session)
    scenes = SceneRepository(db_session)

    await campaigns.create(campaign_id, CampaignCreate(name="Round43 semantic threads"))
    player = await entities.create_character(
        campaign_id,
        CharacterCreate(canonical_name="Алексей"),
    )
    vance = await entities.create_character(
        campaign_id,
        CharacterCreate(canonical_name="Мартин Вэнс"),
    )
    scene = await scenes.create(campaign_id, SceneCreate(title="Архив нотариуса"))
    await scenes.add_participant(scene.id, player.id)
    await scenes.add_participant(scene.id, vance.id)
    await campaigns.update(
        campaign_id,
        CampaignUpdate(
            current_scene_id=scene.id,
            player_character_id=player.id,
        ),
    )

    first_turn = await _assistant_turn(
        db_session,
        campaign_id,
        scene.id,
        "В папке лежат три стёртых следа, латунный ключ и документы Вэнса.",
    )
    curator = ThesisCurator(db_session)
    created = await curator.reconcile(
        scene.id,
        first_turn,
        [
            DesiredThesis(
                thesis_type=ThesisType.UNRESOLVED_BEAT,
                text="Кто стёр три следа из журнала?",
                priority=8,
                semantic_key="three-erased-tracks",
            ),
            DesiredThesis(
                thesis_type=ThesisType.UNRESOLVED_BEAT,
                text="Что открывает латунный ключ?",
                priority=7,
                semantic_key="brass-key-purpose",
            ),
            DesiredThesis(
                thesis_type=ThesisType.UNRESOLVED_BEAT,
                text="Почему ипотека Вэнса расходится с документами?",
                priority=6,
                semantic_key="vance-mortgage-discrepancy",
            ),
            DesiredThesis(
                thesis_type=ThesisType.RELATIONSHIP_DYNAMIC,
                text="Вэнс нервничает, когда Алексей спрашивает о папке.",
                priority=5,
                related_entity_ids=[vance.id],
                semantic_key="vance-pressure",
            ),
        ],
    )
    await db_session.commit()

    assert created.created == 4
    active = await scenes.list_theses_by_scene(scene.id, active_only=True)
    assert len(active) == 4
    by_text = {item.text: item for item in active}

    snapshot = await MemoryOperationsService(db_session).snapshot(campaign_id)
    assert snapshot["health"]["stale_or_duplicate_theses"] == 0

    second_turn = await _assistant_turn(
        db_session,
        campaign_id,
        scene.id,
        "Вэнс признаёт, что ключ открывает его личный сейф, но уходит от вопроса об ипотеке.",
    )
    vance_old = by_text["Вэнс нервничает, когда Алексей спрашивает о папке."]
    brass_key = by_text["Что открывает латунный ключ?"]
    result = await curator.reconcile(
        scene.id,
        second_turn,
        [
            DesiredThesis(
                thesis_type=ThesisType.RELATIONSHIP_DYNAMIC,
                text="Вэнс явно избегает ответа об ипотеке и документах.",
                priority=8,
                related_entity_ids=[vance.id],
                existing_thesis_id=vance_old.id,
                semantic_key="vance-pressure",
            )
        ],
        resolve_thesis_ids={brass_key.id},
    )
    await db_session.commit()

    assert result.resolved == 1
    assert result.superseded == 1
    active = await scenes.list_theses_by_scene(scene.id, active_only=True)
    active_texts = {item.text for item in active}
    assert "Кто стёр три следа из журнала?" in active_texts
    assert "Почему ипотека Вэнса расходится с документами?" in active_texts
    assert "Что открывает латунный ключ?" not in active_texts
    assert "Вэнс явно избегает ответа об ипотеке и документах." in active_texts

    snapshot = await MemoryOperationsService(db_session).snapshot(campaign_id)
    assert snapshot["health"]["stale_or_duplicate_theses"] == 0

    messages, meta = await ContextCompiler(db_session).compile_context(
        campaign_id=campaign_id,
        acting_character_id=None,
        scene_id=scene.id,
    )
    context = "\n".join(message.content for message in messages)
    assert "Кто стёр три следа из журнала?" in context
    assert "Почему ипотека Вэнса расходится с документами?" in context
    assert "Вэнс явно избегает ответа об ипотеке и документах." in context
    assert "Что открывает латунный ключ?" not in context
    assert len(meta["included_thesis_ids"]) == 3
