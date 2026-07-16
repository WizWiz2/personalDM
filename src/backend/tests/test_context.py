import pytest
from uuid import uuid4
from sqlalchemy.ext.asyncio import AsyncSession
from app.services.context_compiler import ContextCompiler
from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.fact_repo import FactRepository
from app.db.repositories.belief_repo import BeliefRepository
from app.models.campaign import CampaignCreate
from app.models.scene import SceneCreate
from app.models.entity import EntityCreate, EntityType
from app.models.character import CharacterCreate
from app.models.fact import FactCreate
from app.models.belief import BeliefCreate

@pytest.mark.asyncio
async def test_knowledge_boundary_leak_protection(db_session: AsyncSession):
    # 1. Initialize repositories and service
    campaign_repo = CampaignRepository(db_session)
    scene_repo = SceneRepository(db_session)
    entity_repo = EntityRepository(db_session)
    fact_repo = FactRepository(db_session)
    belief_repo = BeliefRepository(db_session)
    compiler = ContextCompiler(db_session)

    campaign_id = uuid4()
    
    # 2. Create campaign
    await campaign_repo.create(campaign_id, CampaignCreate(name="Monastery Secrets"))

    # 3. Create characters
    safira = await entity_repo.create_character(
        campaign_id,
        CharacterCreate(
            entity_type=EntityType.CHARACTER,
            canonical_name="Safira",
            description="A royal guard captain"
        )
    )
    
    liara = await entity_repo.create_character(
        campaign_id,
        CharacterCreate(
            entity_type=EntityType.CHARACTER,
            canonical_name="Liara",
            description="A rebel commander"
        )
    )

    # 4. Create secret fact: King is alive
    secret_fact = await fact_repo.create(
        campaign_id,
        FactCreate(
            subject="King",
            predicate="is_status",
            object_value="alive_in_monastery",
            truth_status="true",
            visibility="dm"
        )
    )

    # 5. Safira knows the King is alive
    await belief_repo.create(
        BeliefCreate(
            character_id=safira.id,
            fact_id=secret_fact.id,
            proposition="The King is alive and hiding in the monastery",
            status="known",
            visibility="dm"
        )
    )

    # 6. Liara falsely believes the King is dead
    await belief_repo.create(
        BeliefCreate(
            character_id=liara.id,
            fact_id=None,
            proposition="The King is dead, killed in the coup",
            status="believed",
            visibility="dm"
        )
    )

    # 7. Create scene with both participants
    scene = await scene_repo.create(campaign_id, SceneCreate(title="Monastery Courtyard"))
    await scene_repo.add_participant(scene.id, safira.id)
    await scene_repo.add_participant(scene.id, liara.id)

    # Commit transactions
    await db_session.commit()

    # 8. Compile context for LIARA (she should NOT leak Safira's knowledge)
    liara_messages, liara_meta = await compiler.compile_context(
        campaign_id=campaign_id,
        acting_character_id=liara.id,
        scene_id=scene.id
    )

    # Convert messages to a single string for easy assertions
    liara_context_str = "".join([m.content for m in liara_messages])
    
    # Assertions for Liara
    assert "The King is dead, killed in the coup" in liara_context_str
    assert "The King is alive and hiding in the monastery" not in liara_context_str

    # 9. Compile context for SAFIRA (she should have the secret knowledge)
    safira_messages, safira_meta = await compiler.compile_context(
        campaign_id=campaign_id,
        acting_character_id=safira.id,
        scene_id=scene.id
    )

    safira_context_str = "".join([m.content for m in safira_messages])
    
    # Assertions for Safira
    assert "The King is alive and hiding in the monastery" in safira_context_str
    assert "The King is dead, killed in the coup" not in safira_context_str
