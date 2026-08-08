from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.application import GameApplication
from app.db.repositories.belief_repo import BeliefRepository
from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.models.belief import BeliefCreate
from app.models.campaign import CampaignCreate, CampaignUpdate
from app.models.character import CharacterCreate
from app.models.turn import ChatMessage
from app.models.turn_authority import PlannedNpcIntroduction, TurnAuthority
from app.services.turn_saga import TurnSaga


def test_narrator_receives_one_typed_authority_without_legacy_plan_contract():
    authority = TurnAuthority(
        campaign_id=uuid4(),
        trigger_turn_id=uuid4(),
        player_character_name="Рэт",
        player_input="Стучу в дверь.",
        scene_disposition="stay",
        present_character_names=["Рэт"],
        allowed_new_npcs=[
            PlannedNpcIntroduction(
                canonical_name="Дежурный фабрики",
                role="дежурный",
                reason="Ответил на прямой стук игрока.",
                temporary_name=True,
            )
        ],
        resolution="conversation",
        observable_consequences=["Дежурный фабрики открывает дверь."],
        canon_constraints=["Шептун отсутствует и не может появиться."],
        narration_guidance=["Ответить коротко и конкретно."],
        ending_hook="Дежурный ждёт вопроса.",
    )

    messages = TurnSaga._inject_authority(
        object(),
        [ChatMessage(role="system", content="Base campaign context")],
        authority,
    )
    system = messages[0].content

    assert "[TYPED TURN AUTHORITY" in system
    assert "[APPROVED TURN PLAN]" not in system
    assert '"canonical_name": "Дежурный фабрики"' in system
    assert '"narration_guidance"' in system
    assert "Ответить коротко и конкретно" in system
    assert "Дежурный ждёт вопроса" in system


@pytest.mark.asyncio
async def test_player_memory_view_includes_beliefs_when_fact_table_is_empty(
    db_session: AsyncSession,
):
    campaign_id = uuid4()
    campaigns = CampaignRepository(db_session)
    entities = EntityRepository(db_session)
    await campaigns.create(campaign_id, CampaignCreate(name="Visible beliefs"))
    player = await entities.create_character(
        campaign_id,
        CharacterCreate(canonical_name="Рэт"),
    )
    await campaigns.update(
        campaign_id,
        CampaignUpdate(player_character_id=player.id),
    )
    await BeliefRepository(db_session).create(
        BeliefCreate(
            character_id=player.id,
            proposition="На подошве убийцы широкая полоса с зубцами.",
            status="known",
            confidence=0.8,
            visibility="character_only",
        )
    )
    await db_session.commit()

    memory = await GameApplication(db_session).list_active_facts(campaign_id)

    assert len(memory) == 1
    assert memory[0].memory_kind == "belief"
    assert memory[0].subject == "Рэт"
    assert "широкая полоса с зубцами" in (memory[0].object_value or "")
