import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import uuid4

from app.db.repositories.belief_repo import BeliefRepository
from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.fact_repo import FactRepository
from app.db.repositories.scene_repo import SceneRepository
from app.models.belief import BeliefCreate
from app.models.campaign import CampaignCreate
from app.models.character import CharacterCreate
from app.models.entity import EntityType
from app.models.fact import FactCreate
from app.models.scene import SceneCreate
from app.models.scene_thesis import SceneThesisCreate, ThesisType
from app.services.context_compiler import ContextCompiler


@pytest.mark.asyncio
async def test_knowledge_boundary_leak_protection(db_session: AsyncSession):
    campaign_repo = CampaignRepository(db_session)
    scene_repo = SceneRepository(db_session)
    entity_repo = EntityRepository(db_session)
    fact_repo = FactRepository(db_session)
    belief_repo = BeliefRepository(db_session)
    compiler = ContextCompiler(db_session)

    campaign_id = uuid4()
    await campaign_repo.create(campaign_id, CampaignCreate(name="Monastery Secrets"))

    safira = await entity_repo.create_character(
        campaign_id,
        CharacterCreate(
            entity_type=EntityType.CHARACTER,
            canonical_name="Safira",
            description="A royal guard captain",
        ),
    )
    liara = await entity_repo.create_character(
        campaign_id,
        CharacterCreate(
            entity_type=EntityType.CHARACTER,
            canonical_name="Liara",
            description="A rebel commander",
        ),
    )

    secret_fact = await fact_repo.create(
        campaign_id,
        FactCreate(
            subject="King",
            predicate="is_status",
            object_value="alive_in_monastery",
            truth_status="true",
            visibility="dm",
        ),
    )
    public_fact = await fact_repo.create(
        campaign_id,
        FactCreate(
            subject="Monastery",
            predicate="weather",
            object_value="heavy rain",
            truth_status="true",
            visibility="public",
        ),
    )

    safira_belief = await belief_repo.create(
        BeliefCreate(
            character_id=safira.id,
            fact_id=secret_fact.id,
            proposition="The King is alive and hiding in the monastery",
            status="known",
            visibility="character_only",
        )
    )
    liara_belief = await belief_repo.create(
        BeliefCreate(
            character_id=liara.id,
            fact_id=None,
            proposition="The King is dead, killed in the coup",
            status="believed",
            visibility="character_only",
        )
    )

    scene = await scene_repo.create(
        campaign_id,
        SceneCreate(title="Monastery Courtyard"),
    )
    await scene_repo.add_participant(scene.id, safira.id)
    await scene_repo.add_participant(scene.id, liara.id)
    private_thesis = await scene_repo.create_thesis(
        scene.id,
        SceneThesisCreate(
            thesis_type=ThesisType.SECRET,
            text="The hidden crypt contains the living King",
            visibility="dm",
        ),
    )
    public_thesis = await scene_repo.create_thesis(
        scene.id,
        SceneThesisCreate(
            thesis_type=ThesisType.TENSION,
            text="The courtyard is tense and rain-soaked",
            visibility="public",
        ),
    )
    await db_session.commit()

    liara_messages, liara_meta = await compiler.compile_context(
        campaign_id=campaign_id,
        acting_character_id=liara.id,
        scene_id=scene.id,
        current_user_content="Liara, what do you believe happened to the King?",
    )
    liara_context = "\n".join(message.content for message in liara_messages)

    assert "The King is dead, killed in the coup" in liara_context
    assert "The King is alive and hiding in the monastery" not in liara_context
    assert "alive_in_monastery" not in liara_context
    assert "The hidden crypt contains the living King" not in liara_context
    assert "The courtyard is tense and rain-soaked" in liara_context
    assert "heavy rain" in liara_context
    assert liara_meta["actor_scope_strict"] is True
    assert str(secret_fact.id) not in liara_meta["included_fact_ids"]
    assert str(public_fact.id) in liara_meta["included_fact_ids"]
    assert str(liara_belief.id) in liara_meta["included_belief_ids"]
    assert str(safira_belief.id) not in liara_meta["included_belief_ids"]
    assert str(private_thesis.id) not in liara_meta["included_thesis_ids"]
    assert str(public_thesis.id) in liara_meta["included_thesis_ids"]

    safira_messages, safira_meta = await compiler.compile_context(
        campaign_id=campaign_id,
        acting_character_id=safira.id,
        scene_id=scene.id,
        current_user_content="Safira, what do you know?",
    )
    safira_context = "\n".join(message.content for message in safira_messages)
    assert "The King is alive and hiding in the monastery" in safira_context
    assert "The King is dead, killed in the coup" not in safira_context
    assert "alive_in_monastery" not in safira_context
    assert str(safira_belief.id) in safira_meta["included_belief_ids"]

    narrator_messages, narrator_meta = await compiler.compile_context(
        campaign_id=campaign_id,
        acting_character_id=None,
        scene_id=scene.id,
    )
    narrator_context = "\n".join(message.content for message in narrator_messages)
    assert "alive_in_monastery" in narrator_context
    assert "The hidden crypt contains the living King" in narrator_context
    assert narrator_meta["actor_scope_strict"] is False
    assert str(secret_fact.id) in narrator_meta["included_fact_ids"]
    assert str(private_thesis.id) in narrator_meta["included_thesis_ids"]
