from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.location_repo import LocationRepository
from app.db.repositories.scene_repo import SceneRepository
from app.models.campaign import CampaignCreate
from app.models.character import CharacterCreate
from app.models.location import LocationCreate
from app.models.proposed_change import ChangeType, ProposedChangeCreate
from app.models.scene import SceneCreate
from app.services.continuity_checker import ContinuityChecker
from app.services.proposal_presence import ProposalPresenceResolver


async def _presence_state(db_session: AsyncSession):
    campaign_id = uuid4()
    await CampaignRepository(db_session).create(
        campaign_id,
        CampaignCreate(name="Proposal presence"),
    )
    locations = LocationRepository(db_session)
    tavern = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Медный Котёл"),
    )
    guild = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Гильдия"),
    )
    entities = EntityRepository(db_session)
    hero = await entities.create_character(
        campaign_id,
        CharacterCreate(canonical_name="Эйдан", current_location_id=tavern.id),
    )
    bartender = await entities.create_character(
        campaign_id,
        CharacterCreate(
            canonical_name="Бармен Роэн",
            aliases=["Роэн"],
            current_location_id=tavern.id,
        ),
    )
    remote = await entities.create_character(
        campaign_id,
        CharacterCreate(
            canonical_name="Купец Тарек",
            current_location_id=guild.id,
        ),
    )
    scenes = SceneRepository(db_session)
    scene = await scenes.create(
        campaign_id,
        SceneCreate(title="Общий зал", location_id=tavern.id),
    )
    await scenes.add_participant(scene.id, hero.id)
    await scenes.add_participant(scene.id, bartender.id)
    return campaign_id, tavern, guild, hero, bartender, remote, scene


@pytest.mark.asyncio
async def test_event_participants_are_backfilled_only_from_named_present_characters(
    db_session: AsyncSession,
):
    campaign_id, tavern, _, hero, bartender, _, scene = await _presence_state(
        db_session
    )
    proposal = ProposedChangeCreate(
        change_type=ChangeType.EVENT,
        payload={
            "event_type": "conversation",
            "description": "Бармен Роэн предупреждает о закрытых воротах.",
            "participant_ids": [],
            "_canon": {
                "outcome_id": "o1",
                "kind": "event",
                "description": "Роэн предупредил героя.",
                "evidence": "Бармен Роэн предупреждает о закрытых воротах",
                "authority": "dm_confirmed",
                "operation": "assert",
                "cardinality": "single",
            },
        },
    )

    enriched = await ProposalPresenceResolver(db_session).enrich(
        campaign_id,
        scene.id,
        [proposal],
    )

    assert enriched[0].payload["participant_ids"] == [str(bartender.id)]
    assert enriched[0].payload["location_id"] == str(tavern.id)
    assert str(hero.id) not in enriched[0].payload["participant_ids"]
    valid, warning = await ContinuityChecker(db_session).validate_change(
        campaign_id,
        enriched[0],
        scene_id=scene.id,
    )
    assert valid is True, warning


@pytest.mark.asyncio
async def test_remote_character_cannot_be_event_participant_or_move_from_scene(
    db_session: AsyncSession,
):
    campaign_id, _, tavern_destination, _, _, remote, scene = await _presence_state(
        db_session
    )
    checker = ContinuityChecker(db_session)

    event = ProposedChangeCreate(
        change_type=ChangeType.EVENT,
        payload={
            "event_type": "arrival",
            "description": "Купец Тарек внезапно отвечает из другой комнаты.",
            "participant_ids": [str(remote.id)],
        },
    )
    valid, warning = await checker.validate_change(
        campaign_id,
        event,
        scene_id=scene.id,
    )
    assert valid is False
    assert "not physically present" in warning

    movement = ProposedChangeCreate(
        change_type=ChangeType.MOVEMENT,
        payload={
            "character_id": str(remote.id),
            "location_id": str(tavern_destination.id),
            "description": "Тарек уходит.",
        },
    )
    valid, warning = await checker.validate_change(
        campaign_id,
        movement,
        scene_id=scene.id,
    )
    assert valid is False
    assert "cannot move from a scene" in warning
