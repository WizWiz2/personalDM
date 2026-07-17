from uuid import UUID

import pytest

from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.scene_repo import SceneRepository
from app.models.campaign import CampaignCreate
from app.models.character import CharacterCreate
from app.models.entity import EntityType
from app.models.scene import SceneCreate
from app.models.scene_thesis import SceneThesisCreate, ThesisType
from app.services.thesis_curator import DesiredThesis, ThesisCurator


@pytest.mark.asyncio
async def test_curator_supersedes_same_scope_and_resolves_obsolete(db_session):
    campaign = await CampaignRepository(db_session).create(CampaignCreate(name="Dynamic theses"))
    scene_repo = SceneRepository(db_session)
    scene = await scene_repo.create(campaign.id, SceneCreate(title="Vault"))
    actor = await EntityRepository(db_session).create_character(
        campaign.id,
        CharacterCreate(
            entity_type=EntityType.CHARACTER,
            canonical_name="Liara",
            personality="Suspicious",
        ),
    )
    await scene_repo.add_participant(scene.id, actor.id)

    old_tension = await scene_repo.create_thesis(
        scene.id,
        SceneThesisCreate(
            thesis_type=ThesisType.TENSION,
            text="Liara cautiously watches the sealed door.",
            related_entity_ids=[actor.id],
        ),
    )
    obsolete = await scene_repo.create_thesis(
        scene.id,
        SceneThesisCreate(
            thesis_type=ThesisType.UNRESOLVED_BEAT,
            text="The sealed door has not yet been opened.",
        ),
    )
    await db_session.commit()

    result = await ThesisCurator(db_session).reconcile(
        scene.id,
        old_tension.id,
        [
            DesiredThesis(
                thesis_type=ThesisType.TENSION,
                text="Liara openly suspects the door is a trap.",
                priority=5,
                related_entity_ids=[actor.id],
            )
        ],
    )
    await db_session.commit()

    active = await scene_repo.list_theses_by_scene(scene.id, active_only=True)
    all_theses = await scene_repo.list_theses_by_scene(scene.id, active_only=False)

    assert result.superseded == 1
    assert result.resolved == 1
    assert len(active) == 1
    assert active[0].text == "Liara openly suspects the door is a trap."
    assert next(item for item in all_theses if item.id == old_tension.id).status == "superseded"
    assert next(item for item in all_theses if item.id == obsolete.id).status == "resolved"


@pytest.mark.asyncio
async def test_pinned_thesis_wins_over_conflicting_automatic_candidate(db_session):
    campaign = await CampaignRepository(db_session).create(CampaignCreate(name="Pinned truth"))
    scene_repo = SceneRepository(db_session)
    scene = await scene_repo.create(campaign.id, SceneCreate(title="Council"))
    pinned = await scene_repo.create_thesis(
        scene.id,
        SceneThesisCreate(
            thesis_type=ThesisType.TENSION,
            text="The council remains outwardly calm.",
            pinned=True,
        ),
    )
    await db_session.commit()

    result = await ThesisCurator(db_session).reconcile(
        scene.id,
        pinned.id,
        [
            DesiredThesis(
                thesis_type=ThesisType.TENSION,
                text="The council erupts into open violence.",
                priority=10,
            )
        ],
    )
    await db_session.commit()

    active = await scene_repo.list_theses_by_scene(scene.id, active_only=True)
    assert result.pinned_conflicts == 1
    assert len(active) == 1
    assert active[0].id == pinned.id
    assert active[0].text == "The council remains outwardly calm."


@pytest.mark.asyncio
async def test_duplicate_scope_is_reduced_to_one_active_thesis(db_session):
    campaign = await CampaignRepository(db_session).create(CampaignCreate(name="No conflicts"))
    scene_repo = SceneRepository(db_session)
    scene = await scene_repo.create(campaign.id, SceneCreate(title="Bridge"))
    source_turn = UUID("00000000-0000-0000-0000-000000000001")

    result = await ThesisCurator(db_session).reconcile(
        scene.id,
        source_turn,
        [
            DesiredThesis(
                thesis_type=ThesisType.VISUAL_STATE,
                text="The bridge is intact but shaking.",
                priority=8,
            ),
            DesiredThesis(
                thesis_type=ThesisType.VISUAL_STATE,
                text="The bridge has completely collapsed.",
                priority=2,
            ),
        ],
    )
    await db_session.commit()

    active = await scene_repo.list_theses_by_scene(scene.id, active_only=True)
    assert result.duplicate_scopes == 1
    assert len(active) == 1
    assert active[0].text == "The bridge is intact but shaking."
