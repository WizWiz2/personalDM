from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.action_sequence_table import ActionSequence, ActionStep
from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.fact_repo import FactRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.tables import Turn
from app.models.campaign import CampaignCreate
from app.models.proposed_change import ChangeType
from app.models.scene import SceneCreate
from app.services.canon_applier import CanonApplier


async def _pair(db_session, campaign_id, scene_id, user_content, assistant_content):
    user = Turn(
        id=str(uuid4()),
        campaign_id=str(campaign_id),
        scene_id=str(scene_id),
        role="user",
        content=user_content,
        status="active",
    )
    assistant = Turn(
        id=str(uuid4()),
        campaign_id=str(campaign_id),
        scene_id=str(scene_id),
        role="assistant",
        content=assistant_content,
        status="active",
        parent_turn_id=None,
    )
    db_session.add(user)
    await db_session.flush()
    assistant.parent_turn_id = user.id
    db_session.add(assistant)
    await db_session.flush()
    return user, assistant


def _step(sequence_id, action_type, outcome, status="completed"):
    return ActionStep(
        sequence_id=sequence_id,
        step_index=0,
        action_type=action_type,
        intent="act",
        resolution="auto_success" if status == "completed" else "blocked",
        status=status,
        observable_outcome=outcome,
    )


@pytest.mark.asyncio
async def test_observation_cannot_supersede_a_completed_outcome_fact(db_session: AsyncSession):
    campaign_id = uuid4()
    await CampaignRepository(db_session).create(campaign_id, CampaignCreate(name="Rank"))
    scene = await SceneRepository(db_session).create(
        campaign_id,
        SceneCreate(title="Комната"),
    )
    user, assistant = await _pair(
        db_session,
        campaign_id,
        scene.id,
        "Мария и Анна, откройте ставни.",
        "Ставни открыты.",
    )
    sequence = ActionSequence(
        campaign_id=str(campaign_id),
        trigger_turn_id=user.id,
        source_scene_id=str(scene.id),
        status="prepared",
        summary="open",
        planned_steps=1,
        completed_steps=1,
    )
    db_session.add(sequence)
    await db_session.flush()
    db_session.add(_step(sequence.id, "service", "Ставни открыты."))
    await db_session.flush()

    applier = CanonApplier(db_session)
    await applier.apply(
        campaign_id,
        ChangeType.FACT,
        {
            "subject": "ставни",
            "predicate": "открыты",
            "object_value": "да",
            "truth_status": "true",
            "visibility": "public",
            "scope": "scene",
            "scene_id": str(scene.id),
            "memory_kind": "scene_state",
        },
        assistant.id and __import__("uuid").UUID(assistant.id),
    )
    opened = await FactRepository(db_session).find_current_by_key(
        campaign_id,
        "ставни",
        "открыты",
        scope="scene",
        scene_id=scene.id,
        memory_kind="scene_state",
    )
    assert len(opened) == 1
    assert opened[0].object_value == "да"

    look_user, look_assistant = await _pair(
        db_session,
        campaign_id,
        scene.id,
        "Опиши ставни.",
        "Ставни закрыты плотно.",
    )
    look_sequence = ActionSequence(
        campaign_id=str(campaign_id),
        trigger_turn_id=look_user.id,
        source_scene_id=str(scene.id),
        status="prepared",
        summary="look",
        planned_steps=1,
        completed_steps=1,
    )
    db_session.add(look_sequence)
    await db_session.flush()
    db_session.add(_step(look_sequence.id, "observation", "Илья осмотрел ставни."))
    await db_session.flush()

    await applier.apply(
        campaign_id,
        ChangeType.FACT,
        {
            "subject": "ставни",
            "predicate": "открыты",
            "object_value": "нет",
            "truth_status": "true",
            "visibility": "public",
            "scope": "scene",
            "scene_id": str(scene.id),
            "memory_kind": "scene_state",
            "operation": "revise",
        },
        __import__("uuid").UUID(look_assistant.id),
    )
    still = await FactRepository(db_session).find_current_by_key(
        campaign_id,
        "ставни",
        "открыты",
        scope="scene",
        scene_id=scene.id,
        memory_kind="scene_state",
    )
    assert [fact.object_value for fact in still] == ["да"]

    close_user, close_assistant = await _pair(
        db_session,
        campaign_id,
        scene.id,
        "Закройте ставни.",
        "Ставни снова закрыты.",
    )
    close_sequence = ActionSequence(
        campaign_id=str(campaign_id),
        trigger_turn_id=close_user.id,
        source_scene_id=str(scene.id),
        status="prepared",
        summary="close",
        planned_steps=1,
        completed_steps=1,
    )
    db_session.add(close_sequence)
    await db_session.flush()
    db_session.add(_step(close_sequence.id, "service", "Ставни закрыты."))
    await db_session.flush()
    await applier.apply(
        campaign_id,
        ChangeType.FACT,
        {
            "subject": "ставни",
            "predicate": "открыты",
            "object_value": "нет",
            "truth_status": "true",
            "visibility": "public",
            "scope": "scene",
            "scene_id": str(scene.id),
            "memory_kind": "scene_state",
            "operation": "revise",
        },
        __import__("uuid").UUID(close_assistant.id),
    )
    closed = await FactRepository(db_session).find_current_by_key(
        campaign_id,
        "ставни",
        "открыты",
        scope="scene",
        scene_id=scene.id,
        memory_kind="scene_state",
    )
    assert [fact.object_value for fact in closed] == ["нет"]


@pytest.mark.asyncio
async def test_established_state_comes_only_from_completed_world_steps(db_session: AsyncSession):
    from app.models.fact import FactCreate
    from app.services.outcome_fact_authority import established_state_lines

    campaign_id = uuid4()
    await CampaignRepository(db_session).create(campaign_id, CampaignCreate(name="State"))
    scene = await SceneRepository(db_session).create(campaign_id, SceneCreate(title="Комната"))
    user, assistant = await _pair(db_session, campaign_id, scene.id, "Откройте.", "Открыто.")
    sequence = ActionSequence(
        campaign_id=str(campaign_id),
        trigger_turn_id=user.id,
        source_scene_id=str(scene.id),
        status="prepared",
        planned_steps=1,
        completed_steps=1,
    )
    db_session.add(sequence)
    await db_session.flush()
    db_session.add(_step(sequence.id, "service", "Ставни открыты."))
    look_user, look_assistant = await _pair(db_session, campaign_id, scene.id, "Опиши.", "Закрыты.")
    look_sequence = ActionSequence(
        campaign_id=str(campaign_id),
        trigger_turn_id=look_user.id,
        source_scene_id=str(scene.id),
        status="prepared",
        planned_steps=1,
        completed_steps=1,
    )
    db_session.add(look_sequence)
    await db_session.flush()
    db_session.add(_step(look_sequence.id, "observation", "Осмотр."))
    await db_session.flush()
    facts = FactRepository(db_session)
    from uuid import UUID
    await facts.create(
        campaign_id,
        FactCreate(
            subject="ставни",
            predicate="открыты",
            object_value="да",
            visibility="public",
            scope="scene",
            scene_id=scene.id,
            memory_kind="scene_state",
            source_turn_id=UUID(assistant.id),
        ),
    )
    await facts.create(
        campaign_id,
        FactCreate(
            subject="свет",
            predicate="слабый",
            object_value="да",
            visibility="public",
            scope="scene",
            scene_id=scene.id,
            memory_kind="scene_state",
            source_turn_id=UUID(look_assistant.id),
        ),
    )
    lines = await established_state_lines(db_session, campaign_id, scene.id)
    assert lines == ["Ставни открыты."]


def test_publication_fallback_keeps_established_state():
    from app.models.turn_authority import TurnAuthority
    from app.services.narration_publication_guard import NarrationPublicationGuard

    authority = TurnAuthority(
        campaign_id=uuid4(),
        trigger_turn_id=uuid4(),
        player_input="Опиши ставни.",
        observable_consequences=["Илья осмотрел ставни."],
        established_state=["ставни открыты да"],
    )
    text = NarrationPublicationGuard.render_authority(authority)
    assert "ставни открыты да" in text


def test_observation_outcome_does_not_override_established_state():
    from app.models.turn_authority import TurnAuthority
    from app.services.narration_publication_guard import NarrationPublicationGuard

    lie = "Ставни были плотно закрыты деревянными планками."
    authority = TurnAuthority(
        campaign_id=uuid4(),
        trigger_turn_id=uuid4(),
        player_input="Опиши ставни.",
        observable_consequences=[lie],
        established_state=["ставни открыты да"],
        action_sequence={
            "steps": [
                {
                    "action_type": "observation",
                    "status": "completed",
                    "observable_outcome": lie,
                }
            ]
        },
    )
    text = NarrationPublicationGuard.render_authority(authority)
    assert "закрыт" not in text
    assert "ставни открыты да" in text


def test_observation_publication_yields_to_established_state():
    from app.models.narration_validation import NarrationValidationResult
    from app.models.turn_authority import TurnAuthority
    from app.services.narration_publication_guard import NarrationPublicationGuard

    lie = "Деревянные ставни закрывали яркий свет, и в комнате стоял полумрак. На столе стоит кувшин с водой."
    authority = TurnAuthority(
        campaign_id=uuid4(),
        trigger_turn_id=uuid4(),
        player_input="Опиши ставни.",
        resolution="auto_success",
        observable_consequences=["Илья осмотрел окна и кувшин."],
        established_state=["ставни открыты да"],
        established_subjects=["ставни"],
        action_sequence={
            "steps": [
                {
                    "action_type": "observation",
                    "status": "completed",
                    "observable_outcome": "Илья осмотрел окна и кувшин.",
                }
            ]
        },
    )
    published, guard = NarrationPublicationGuard.publish(
        authority,
        lie,
        NarrationValidationResult(verdict="pass", summary="ok", violations=[]),
    )
    assert guard["candidate_discarded"] is True
    assert "закрывал" not in published
    assert "полумрак" not in published
    assert "ставни открыты да" in published
    assert "кувшин с водой" in published


def test_non_observation_keeps_validated_prose_beside_established_state():
    from app.models.narration_validation import NarrationValidationResult
    from app.models.turn_authority import TurnAuthority
    from app.services.narration_publication_guard import NarrationPublicationGuard

    prose = "Мария и Анна открыли ставни, и в комнату вошел свет."
    authority = TurnAuthority(
        campaign_id=uuid4(),
        trigger_turn_id=uuid4(),
        player_input="Откройте ставни.",
        resolution="auto_success",
        observable_consequences=["Мария и Анна открыли ставни."],
        established_state=["ставни открыты да"],
        action_sequence={
            "steps": [
                {
                    "action_type": "service",
                    "status": "completed",
                    "observable_outcome": "Мария и Анна открыли ставни.",
                }
            ]
        },
    )
    published, guard = NarrationPublicationGuard.publish(
        authority,
        prose,
        NarrationValidationResult(verdict="pass", summary="ok", violations=[]),
    )
    assert guard["candidate_discarded"] is False
    assert published == prose
