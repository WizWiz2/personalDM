from types import SimpleNamespace
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
from app.models.proposed_change import ChangeType, ProposedChangeCreate
from app.models.scene import SceneCreate
from app.services.actor_turn_authority_guard import segment_actor_response
from app.services.memory_scribe import MemoryScribe
from app.services.narrator_memory_audit_guard import enrich_narrator_memory


async def _world(db_session: AsyncSession):
    campaign_id = uuid4()
    campaigns = CampaignRepository(db_session)
    entities = EntityRepository(db_session)
    locations = LocationRepository(db_session)
    scenes = SceneRepository(db_session)

    await campaigns.create(campaign_id, CampaignCreate(name="Round 44 memory semantics"))
    office = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Контора Мартина"),
    )
    hero = await entities.create_character(
        campaign_id,
        CharacterCreate(canonical_name="Илья", current_location_id=office.id),
    )
    martin = await entities.create_character(
        campaign_id,
        CharacterCreate(
            canonical_name="Мартин Вэнс",
            aliases=["Мартин"],
            current_location_id=office.id,
        ),
    )
    await campaigns.update(campaign_id, CampaignUpdate(player_character_id=hero.id))
    scene = await scenes.create(
        campaign_id,
        SceneCreate(title="Разговор в конторе", location_id=office.id),
    )
    await scenes.add_participant(scene.id, hero.id, allow_movement=True)
    await scenes.add_participant(scene.id, martin.id, allow_movement=True)
    await db_session.commit()
    return campaign_id, hero, martin, scene


def _base_wrong_claim_fact(claim: str) -> ProposedChangeCreate:
    return ProposedChangeCreate(
        change_type=ChangeType.FACT,
        payload={
            "subject": "Ипотека",
            "predicate": "покрывает",
            "object_value": "всё, что числится за складом",
            "truth_status": "true",
            "visibility": "public",
            "scope": "campaign",
            "operation": "assert",
            "cardinality": "single",
            "_canon": {
                "outcome_id": "bad-claim",
                "kind": "world_state",
                "description": claim,
                "evidence": claim,
                "authority": "dm_confirmed",
                "operation": "assert",
                "cardinality": "single",
                "durable": True,
            },
        },
    )


@pytest.mark.asyncio
async def test_narrator_memory_audit_separates_npc_claims_and_recovers_plot_facts(
    db_session: AsyncSession,
):
    campaign_id, hero, martin, scene = await _world(db_session)
    published = (
        "Мартин Вэнс качает головой. «Ипотека покрывает всё, что числится за складом». "
        "На столе лежит латунный ключ с номером 17. "
        "В папке три фотографии и квитанция."
    )
    claim = "Ипотека покрывает всё, что числится за складом"
    segments = segment_actor_response(published)
    claim_segment_id = next(
        index
        for index, segment in enumerate(segments, start=1)
        if segment == claim
    )

    scribe = MemoryScribe(db_session)
    scribe._model_router.resolve = AsyncMock(return_value=SimpleNamespace())
    scribe._model_router.generate_json = AsyncMock(
        return_value={
            "claims": [
                {
                    "segment_id": claim_segment_id,
                    "speaker_name": "Мартин Вэнс",
                }
            ],
            "recovery": {
                "outcomes": [
                    {
                        "id": "key17",
                        "kind": "world_state",
                        "description": "На столе обнаружен латунный ключ с номером 17.",
                        "evidence": "На столе лежит латунный ключ с номером 17",
                        "authority": "public_observation",
                        "durable": True,
                    },
                    {
                        "id": "folder",
                        "kind": "world_state",
                        "description": "В папке лежат три фотографии и квитанция.",
                        "evidence": "В папке три фотографии и квитанция",
                        "authority": "public_observation",
                        "durable": True,
                    },
                ],
                "proposals": [
                    {
                        "outcome_id": "key17",
                        "change_type": "fact",
                        "operation": "assert",
                        "cardinality": "single",
                        "payload": {
                            "subject": "Латунный ключ",
                            "predicate": "номер",
                            "object_value": "17",
                            "truth_status": "true",
                            "visibility": "public",
                            "scope": "scene",
                        },
                    },
                    {
                        "outcome_id": "folder",
                        "change_type": "fact",
                        "operation": "assert",
                        "cardinality": "multi",
                        "payload": {
                            "subject": "Папка",
                            "predicate": "содержит",
                            "object_value": "три фотографии и квитанцию",
                            "truth_status": "true",
                            "visibility": "public",
                            "scope": "scene",
                        },
                    },
                ],
            },
        }
    )

    proposals = await enrich_narrator_memory(
        scribe,
        campaign_id=campaign_id,
        scene_id=scene.id,
        assistant_content=published,
        player_character_id=hero.id,
        base_proposals=[_base_wrong_claim_fact(claim)],
    )

    knowledge = [item for item in proposals if item.change_type == ChangeType.KNOWLEDGE]
    facts = [item for item in proposals if item.change_type == ChangeType.FACT]

    assert len(knowledge) == 1
    assert knowledge[0].payload["source_character_id"] == str(martin.id)
    assert knowledge[0].payload["recipient_id"] == str(hero.id)
    assert knowledge[0].payload["proposition"] == claim
    assert knowledge[0].payload["_canon"]["authority"] == "character_claim"

    assert {item.payload["subject"] for item in facts} == {"Латунный ключ", "Папка"}
    assert all(item.payload["scope"] == "scene" for item in facts)
    assert all(item.payload.get("scene_id") == str(scene.id) for item in facts)
    assert not any(item.payload.get("subject") == "Ипотека" for item in facts)

    audit = scribe.last_audit
    assert audit["narrator_memory_auditor"] == "completed"
    assert audit["narrator_claim_count"] == 1
    assert audit["objective_recovery_count"] == 2
    assert audit["claim_promotions_removed"] == 1


def test_memory_audit_contract_never_rewrites_claim_text():
    from app.services.narrator_memory_audit_guard import _AUDIT_PROMPT

    assert "SEGMENT ID" in _AUDIT_PROMPT
    assert "never rewrite the claim text" in _AUDIT_PROMPT
    assert "never objective" in _AUDIT_PROMPT
    assert "missing from EXISTING SCRIBE PROPOSALS" in _AUDIT_PROMPT
