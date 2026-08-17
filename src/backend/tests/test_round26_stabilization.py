from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.proposed_change_repo import ProposedChangeRepository
from app.db.repositories.turn_repo import TurnRepository
from app.models.campaign import CampaignCreate, CampaignUpdate
from app.models.character import CharacterCreate
from app.models.narration_validation import NarrationValidationResult
from app.models.proposed_change import ChangeType, ProposedChangeCreate
from app.models.turn import ChatMessage, TurnCreate
from app.models.turn_authority import PlannedNpcIntroduction, TurnAuthority
from app.services.context_compiler import ContextCompiler
from app.services.entity_identity import resolve_character_candidates
from app.services.turn_authority_service import TurnAuthorityService
from app.services.turn_authority_validator import TurnAuthorityValidator
from app.services.turn_runner import TurnRunner


def _pass_result() -> NarrationValidationResult:
    return NarrationValidationResult(verdict="pass", summary="ok", violations=[])


def _stationary_authority() -> TurnAuthority:
    return TurnAuthority(
        campaign_id=uuid4(),
        trigger_turn_id=uuid4(),
        player_character_name="Виктор Соколов",
        player_input="Осматриваю улицу.",
        source_location_path=["Город", "Окрестности"],
        target_location_path=["Город", "Окрестности"],
        scene_disposition="stay",
        transition_type="none",
        present_character_names=["Виктор Соколов"],
    )


@pytest.mark.asyncio
async def test_talk_target_reaches_planner_as_addressee_not_actor(db_session):
    irina_id = uuid4()
    routed = TurnRunner._route_addressed_input(
        TurnCreate(
            role="user",
            content="Ирина, я слушаю.",
            acting_character_id=irina_id,
        )
    )
    runner = TurnRunner.__new__(TurnRunner)
    runner._session = db_session
    compiled = ([ChatMessage(role="system", content="base")], {"token_budget_max": 4096})

    with patch.object(
        ContextCompiler,
        "compile_context",
        new_callable=AsyncMock,
        return_value=compiled,
    ) as compile_context:
        await runner._compile(
            uuid4(),
            routed,
            None,
            SimpleNamespace(context_window=8192),
        )

    kwargs = compile_context.await_args.kwargs
    assert kwargs["acting_character_id"] is None
    assert kwargs["addressed_character_id"] == irina_id


@pytest.mark.asyncio
async def test_addressed_target_contract_keeps_player_as_input_actor(db_session):
    campaign_id = uuid4()
    campaigns = CampaignRepository(db_session)
    entities = EntityRepository(db_session)
    await campaigns.create(campaign_id, CampaignCreate(name="routing contract"))
    player = await entities.create_character(
        campaign_id,
        CharacterCreate(canonical_name="Виктор Соколов"),
    )
    irina = await entities.create_character(
        campaign_id,
        CharacterCreate(canonical_name="Ирина"),
    )
    await campaigns.update(campaign_id, CampaignUpdate(player_character_id=player.id))

    compiler = ContextCompiler(db_session)
    messages, metadata = await compiler._apply_addressed_target_contract(
        campaign_id,
        irina.id,
        None,
        [ChatMessage(role="system", content="BASE")],
        {},
    )

    system = messages[0].content
    assert "Виктор Соколов is the actor/speaker" in system
    assert "Ирина is only the addressed listener/interaction target" in system
    assert "ACTOR OUTPUT CONTRACT: Ирина" not in system
    assert metadata["addressed_character_id"] == str(irina.id)
    assert metadata["input_actor_character_id"] == str(player.id)


def test_validator_rejects_arrival_without_structured_transition():
    result = TurnAuthorityValidator.apply_deterministic_scene_boundaries(
        _pass_result(),
        _stationary_authority(),
        "Ты возвращаешься в офис и закрываешь дверь.",
    )

    assert result.verdict == "repair_required"
    assert any(
        item.violation_type == "invalid_movement" and item.severity == "error"
        for item in result.violations
    )


def test_validator_rejects_unplanned_generic_npc_appearance():
    result = TurnAuthorityValidator.apply_deterministic_scene_boundaries(
        _pass_result(),
        _stationary_authority(),
        "Незнакомец в плаще входит в офис и молча смотрит на тебя.",
    )

    assert result.verdict == "repair_required"
    assert any(
        item.violation_type == "absent_character" and item.severity == "error"
        for item in result.violations
    )


def test_validator_does_not_reject_non_present_reference_to_stranger():
    result = TurnAuthorityValidator.apply_deterministic_scene_boundaries(
        _pass_result(),
        _stationary_authority(),
        "На полу остались следы незнакомца в плаще, которого здесь сейчас нет.",
    )

    assert result.verdict == "pass"


@pytest.mark.asyncio
async def test_safe_fallback_cannot_create_objective_memory(db_session):
    campaign_id = uuid4()
    campaigns = CampaignRepository(db_session)
    turns = TurnRepository(db_session)
    await campaigns.create(campaign_id, CampaignCreate(name="fallback memory"))
    user = await turns.create(
        campaign_id,
        TurnCreate(role="user", content="Осматриваюсь."),
    )
    assistant = await turns.create(
        campaign_id,
        TurnCreate(
            role="assistant",
            content="Незнакомец вошёл в офис.",
            parent_turn_id=user.id,
            context_snapshot={
                "turn_authority": {"scene_disposition": "stay"},
                "interagent_protocol": {
                    "version": 2,
                    "validator_status": "safe_fallback",
                },
            },
        ),
    )

    created = await ProposedChangeRepository(db_session).create_batch(
        assistant.id,
        [
            ProposedChangeCreate(
                change_type=ChangeType.EVENT,
                payload={"description": "Незнакомец вошёл в офис"},
            ),
            ProposedChangeCreate(
                change_type=ChangeType.FACT,
                payload={"subject": "Незнакомец", "predicate": "present"},
            ),
            ProposedChangeCreate(
                change_type=ChangeType.NARRATIVE_DETAIL,
                payload={"text": "В комнате пахнет пылью"},
            ),
        ],
    )

    by_type = {item.change_type: item for item in created}
    assert by_type[ChangeType.EVENT.value].status == "invalid"
    assert by_type[ChangeType.FACT.value].status == "invalid"
    assert "containment" in by_type[ChangeType.EVENT.value].payload["_validation_error"]
    assert by_type[ChangeType.NARRATIVE_DETAIL.value].status == "proposed"


@pytest.mark.asyncio
async def test_temporary_forensic_photographer_can_be_promoted_to_irina(db_session):
    campaign_id = uuid4()
    location_id = uuid4()
    entities = EntityRepository(db_session)
    await CampaignRepository(db_session).create(
        campaign_id,
        CampaignCreate(name="starter identity"),
    )
    starter = await entities.create_character(
        campaign_id,
        CharacterCreate(
            canonical_name="Судебный фотограф",
            custom_fields={
                "temporary_name": True,
                "role": "судебный фотограф",
                "bootstrap_role": "судебный фотограф",
            },
        ),
    )
    proposal = PlannedNpcIntroduction(
        canonical_name="Ирина",
        role="судебный фотограф",
        reason="Игрок узнаёт имя присутствующей фотографа.",
    )

    matches = resolve_character_candidates(
        [await entities.get_by_id(starter.id)],
        proposed_name=proposal.canonical_name,
        proposed_role=proposal.role,
        temporary_name=proposal.temporary_name,
        target_location_id=location_id,
        character_locations={starter.id: location_id},
    )
    assert [item.id for item in matches] == [starter.id]

    promoted = await TurnAuthorityService(db_session)._promote_temporary_identity(
        matches[0],
        proposal,
    )
    assert promoted.canonical_name == "Ирина"
    assert "Судебный фотограф" in promoted.aliases
    assert promoted.custom_fields["temporary_name"] is False
