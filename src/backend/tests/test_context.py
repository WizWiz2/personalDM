import json
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.belief_repo import BeliefRepository
from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.fact_repo import FactRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.tables import Entity, Item
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
            backstory_secret="Safira serves the hidden living King.",
            custom_fields={
                "capabilities": ["command royal guards"],
                "limitations": ["cannot read ancient runes"],
            },
        ),
    )
    liara = await entity_repo.create_character(
        campaign_id,
        CharacterCreate(
            entity_type=EntityType.CHARACTER,
            canonical_name="Liara",
            description="A rebel commander",
            values=["freedom"],
            fears=["betrayal"],
            current_intentions=["question Safira"],
            custom_fields={
                "capabilities": ["read ancient runes"],
                "limitations": ["cannot cast healing magic"],
            },
        ),
    )

    lens_entity = Entity(
        campaign_id=str(campaign_id),
        entity_type="item",
        canonical_name="Brass lens",
        aliases=json.dumps([]),
        status="active",
        provenance="test",
        version=1,
    )
    db_session.add(lens_entity)
    await db_session.flush()
    db_session.add(Item(entity_id=lens_entity.id, current_owner_id=str(liara.id)))

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
    assert "Safira serves the hidden living King" not in liara_context
    assert "command royal guards" not in liara_context
    assert "read ancient runes" in liara_context
    assert "cannot cast healing magic" in liara_context
    assert "Brass lens" in liara_context
    assert "heavy rain" in liara_context
    assert liara_meta["actor_scope_strict"] is True
    assert str(secret_fact.id) not in liara_meta["included_fact_ids"]
    assert str(public_fact.id) in liara_meta["included_fact_ids"]
    assert str(liara_belief.id) in liara_meta["included_belief_ids"]
    assert str(safira_belief.id) not in liara_meta["included_belief_ids"]
    assert str(private_thesis.id) not in liara_meta["included_thesis_ids"]
    assert str(public_thesis.id) in liara_meta["included_thesis_ids"]
    assert str(UUID(lens_entity.id)) in liara_meta["included_item_ids"]

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
    assert "Safira serves the hidden living King" in safira_context
    assert str(safira_belief.id) in safira_meta["included_belief_ids"]

    narrator_messages, narrator_meta = await compiler.compile_context(
        campaign_id=campaign_id,
        acting_character_id=None,
        scene_id=scene.id,
    )
    narrator_context = "\n".join(message.content for message in narrator_messages)
    assert "alive_in_monastery" in narrator_context
    assert "The hidden crypt contains the living King" in narrator_context
    assert "Safira serves the hidden living King" in narrator_context
    assert "command royal guards" in narrator_context
    assert "read ancient runes" in narrator_context
    assert "Brass lens" in narrator_context
    assert narrator_meta["actor_scope_strict"] is False
    assert str(secret_fact.id) in narrator_meta["included_fact_ids"]
    assert str(private_thesis.id) in narrator_meta["included_thesis_ids"]


@pytest.mark.asyncio
async def test_narrator_card_carries_what_a_present_npc_already_said(db_session: AsyncSession):
    """Replay 4: T5 «смотрителя сейчас нет» reached the narrator only as the hero's anonymous memory."""
    campaign_id = uuid4()
    await CampaignRepository(db_session).create(campaign_id, CampaignCreate(name="Memory"))
    entities, scenes = EntityRepository(db_session), SceneRepository(db_session)
    hero = await entities.create_character(campaign_id, CharacterCreate(canonical_name="Илья"))
    host = await entities.create_character(campaign_id, CharacterCreate(canonical_name="Семён"))
    scene = await scenes.create(campaign_id, SceneCreate(title="Трактир"))
    for person in (hero, host):
        await scenes.add_participant(scene.id, person.id)
    for proposition, source in [("Смотрителя сейчас нет.", host.id), ("Огни видели с парохода.", None)]:
        await BeliefRepository(db_session).create(BeliefCreate(
            character_id=hero.id, proposition=proposition, source_character_id=source,
            status="known", visibility="character_only",
        ))
    await db_session.commit()

    messages, _ = await ContextCompiler(db_session).compile_context(campaign_id, scene_id=scene.id)
    context = "\n".join(message.content for message in messages)

    host_card = context[context.index("Семён"):]
    assert "Already said by this character" in host_card
    assert host_card.index("Already said") < host_card.index("Смотрителя сейчас нет.")
    assert context.count("Смотрителя сейчас нет.") == 1
    assert "Огни видели с парохода." in context


@pytest.mark.asyncio
async def test_a_line_heard_from_an_absent_npc_keeps_its_speaker(db_session: AsyncSession):
    """Live B5 T4: the host took the absent fisherman's «— Степаном меня зовут.» as his own."""
    campaign_id = uuid4()
    await CampaignRepository(db_session).create(campaign_id, CampaignCreate(name="Heard"))
    entities, scenes = EntityRepository(db_session), SceneRepository(db_session)
    hero = await entities.create_character(campaign_id, CharacterCreate(canonical_name="Илья"))
    fisher = await entities.create_character(campaign_id, CharacterCreate(canonical_name="Рыбак"))
    scene = await scenes.create(campaign_id, SceneCreate(title="Трактир"))
    await scenes.add_participant(scene.id, hero.id)
    await BeliefRepository(db_session).create(BeliefCreate(
        character_id=hero.id, proposition="— Степаном меня зовут.", source_character_id=fisher.id,
        status="known", visibility="character_only",
    ))
    await db_session.commit()

    messages, _ = await ContextCompiler(db_session).compile_context(campaign_id, scene_id=scene.id)

    assert "- heard from Рыбак: — Степаном меня зовут." in messages[0].content


@pytest.mark.asyncio
async def test_narrator_gets_the_typed_campaign_narrative_person(db_session: AsyncSession):
    """Replay 4 drifted between «Илья…» and «вы»; the person is one typed campaign setting."""
    from app.db.repositories.campaign_setup_repo import CampaignSetupRepository

    campaign_id = uuid4()
    await CampaignRepository(db_session).create(campaign_id, CampaignCreate(name="Person"))
    compiler = ContextCompiler(db_session)
    default, _ = await compiler.compile_context(campaign_id)
    assert "Narrative person: narrate the protagonist in second person singular («ты»)" in default[0].content

    setups = CampaignSetupRepository(db_session)
    row = await setups.create_draft(campaign_id, campaign_name="Person")
    await setups.update(row, {"custom_fields": {"narrative_person": "second_plural"}})
    plural, _ = await compiler.compile_context(campaign_id)
    assert "second person plural («вы»)" in plural[0].content

    await setups.update(row, {"custom_fields": {"narrative_person": "вы"}})
    with pytest.raises(ValueError):
        await compiler.compile_context(campaign_id)
