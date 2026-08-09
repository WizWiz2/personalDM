import json
from datetime import datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.location_repo import LocationRepository
from app.db.repositories.scene_repo import SceneRepository
from app.models.campaign import CampaignCreate, CampaignUpdate
from app.models.character import CharacterCreate
from app.models.location import LocationCreate
from app.models.scene import SceneCreate
from app.models.turn import TurnRead
from app.models.turn_authority import PlannedNpcIntroduction
from app.services.canon_semantics import evidence_supported
from app.services.entity_identity import identity_key
from app.services.entity_registrar import EntityRegistrar
from app.services.post_turn_processor import PostTurnProcessor
from app.services.turn_authority_planner import CoordinatedTurnPlan
from app.services.turn_authority_service import TurnAuthorityError, TurnAuthorityService
from app.services.turn_outcome_materializer import TurnOutcomeMaterializer


pytestmark = pytest.mark.interagent_contract_enforced


def _temporary_contact(name: str, role: str) -> CoordinatedTurnPlan:
    return CoordinatedTurnPlan(
        player_intent=f"Поговорить с {name}.",
        resolution="conversation",
        npc_introductions=[
            PlannedNpcIntroduction(
                canonical_name=name,
                role=role,
                reason="Игрок явно ищет этого человека в его обычной локации.",
                temporary_name=True,
            )
        ],
        observable_consequences=[f"{name} доступен для разговора."],
        ending_hook=f"{name} ждёт вопроса.",
    )


async def _identity_campaign(db_session: AsyncSession, *, two_owners: bool = False):
    campaign_id = uuid4()
    campaigns = CampaignRepository(db_session)
    entities = EntityRepository(db_session)
    locations = LocationRepository(db_session)
    scenes = SceneRepository(db_session)

    await campaigns.create(campaign_id, CampaignCreate(name="Round 6 identity"))
    tavern = await locations.create(
        campaign_id,
        LocationCreate(canonical_name='Таверна "Гнилой фонарь"'),
    )
    player = await entities.create_character(
        campaign_id,
        CharacterCreate(canonical_name="Рэт Уайтмоур", current_location_id=tavern.id),
    )
    owner = await entities.create_character(
        campaign_id,
        CharacterCreate(
            canonical_name="Хозяин таверны",
            aliases=[],
            description="Хозяин и трактирщик заведения.",
            current_location_id=tavern.id,
        ),
    )
    if two_owners:
        await entities.create_character(
            campaign_id,
            CharacterCreate(
                canonical_name="Старший трактирщик",
                description="Трактирщик второй смены.",
                current_location_id=tavern.id,
            ),
        )
    await campaigns.update(
        campaign_id,
        CampaignUpdate(player_character_id=player.id),
    )
    scene = await scenes.create(
        campaign_id,
        SceneCreate(title="Возвращение в таверну", location_id=tavern.id),
    )
    await scenes.add_participant(scene.id, player.id, allow_movement=True)
    await db_session.commit()
    return campaign_id, tavern, player, owner, scene


def test_identity_key_normalizes_qwen_mixed_script_names():
    assert identity_key("Эйдан") == identity_key("Эйdan")
    assert identity_key("Рэт Уайтмоур") == identity_key("Rэт Уайтмоур")
    assert identity_key("Хозяин   таверны") == identity_key("хозяин таверны")


def test_turn_read_keeps_internal_authority_snapshot_without_serializing_it():
    turn = TurnRead(
        id=uuid4(),
        campaign_id=uuid4(),
        scene_id=uuid4(),
        acting_character_id=None,
        role="assistant",
        content="Текст.",
        parent_turn_id=uuid4(),
        status="active",
        model_name="qwen2.5:7b",
        token_count=20,
        created_at=datetime.utcnow(),
        context_snapshot=json.dumps(
            {
                "turn_authority": {"version": 1},
                "interagent_protocol": {"version": 2},
            }
        ),
    )

    assert PostTurnProcessor._authority_managed(turn) is True
    assert "context_snapshot" not in turn.model_dump()


@pytest.mark.asyncio
async def test_round6_temporary_innkeeper_reuses_owner_without_alias(
    db_session: AsyncSession,
):
    campaign_id, _tavern, _player, owner, scene = await _identity_campaign(db_session)
    plan = _temporary_contact("Трактирщик", "трактирщик")

    authority = await TurnAuthorityService(db_session).build(
        campaign_id=campaign_id,
        trigger_turn_id=uuid4(),
        player_input="Возвращаюсь в таверну и хочу поговорить с трактирщиком.",
        source_scene_id=scene.id,
        target_scene_id=scene.id,
        plan=plan,
        acting_character_id=None,
    )

    assert authority.allowed_new_npcs == []
    assert authority.allowed_existing_npc_arrival_names == ["Хозяин таверны"]

    before = await EntityRepository(db_session).list_by_campaign(
        campaign_id,
        entity_type="character",
    )
    outcome = await TurnOutcomeMaterializer(db_session).materialize(
        authority,
        source_turn_id=uuid4(),
    )
    await db_session.commit()
    after = await EntityRepository(db_session).list_by_campaign(
        campaign_id,
        entity_type="character",
    )

    assert len(after) == len(before)
    assert outcome.introduced_character_ids == ()
    assert outcome.arrived_existing_character_ids == (owner.id,)
    assert owner.id in await SceneRepository(db_session).get_participants(scene.id)


@pytest.mark.asyncio
async def test_generic_role_match_fails_closed_when_same_location_is_ambiguous(
    db_session: AsyncSession,
):
    campaign_id, _tavern, _player, _owner, scene = await _identity_campaign(
        db_session,
        two_owners=True,
    )

    with pytest.raises(TurnAuthorityError, match="ambiguous"):
        await TurnAuthorityService(db_session).build(
            campaign_id=campaign_id,
            trigger_turn_id=uuid4(),
            player_input="Хочу поговорить с трактирщиком.",
            source_scene_id=scene.id,
            target_scene_id=scene.id,
            plan=_temporary_contact("Трактирщик", "трактирщик"),
            acting_character_id=None,
        )


@pytest.mark.asyncio
async def test_entity_registrar_cannot_clone_player_from_mixed_script(
    db_session: AsyncSession,
):
    campaign_id = uuid4()
    campaigns = CampaignRepository(db_session)
    entities = EntityRepository(db_session)
    locations = LocationRepository(db_session)
    scenes = SceneRepository(db_session)

    await campaigns.create(campaign_id, CampaignCreate(name="Registrar identity"))
    entrance = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Служебный вход Купцов"),
    )
    player = await entities.create_character(
        campaign_id,
        CharacterCreate(canonical_name="Эйдан", current_location_id=entrance.id),
    )
    await campaigns.update(
        campaign_id,
        CampaignUpdate(player_character_id=player.id),
    )
    scene = await scenes.create(
        campaign_id,
        SceneCreate(title="Утро у входа Купцов", location_id=entrance.id),
    )
    await scenes.add_participant(scene.id, player.id, allow_movement=True)
    await db_session.commit()

    assistant_content = (
        "Теперь Эйдан стоит у служебного входа Купцов; площадь вокруг тиха."
    )
    mixed_evidence = "Эйdan стоит у служебного входа Купцов."
    assert evidence_supported(mixed_evidence, assistant_content) is True

    registrar = EntityRegistrar(db_session)
    registrar._router.resolve = AsyncMock(return_value=object())
    registrar._router.generate_json = AsyncMock(
        return_value={
            "characters": [
                {
                    "canonical_name": "Эйdan",
                    "aliases": [],
                    "role": "главный герой",
                    "evidence": mixed_evidence,
                    "presence": "present",
                    "importance": "major",
                    "temporary_name": False,
                    "persistent": True,
                }
            ]
        }
    )

    result = await registrar.register_from_turn(
        campaign_id,
        scene.id,
        uuid4(),
        assistant_content,
    )
    await db_session.commit()

    characters = await entities.list_by_campaign(campaign_id, entity_type="character")
    assert result.created_ids == []
    assert result.resolved_ids == []
    assert [character.canonical_name for character in characters] == ["Эйдан"]
    assert await scenes.get_participants(scene.id) == [player.id]
