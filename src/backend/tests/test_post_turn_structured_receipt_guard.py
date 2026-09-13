import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.config import settings
from app.db.tables import Campaign, PostTurnJob, ProposedChange, RelationshipAssertion, Turn
from app.services.post_turn_processor import PostTurnProcessor
from app.services.post_turn_structured_receipt_guard import (
    RelationshipReceiptDecision,
    _ensure_relationship_receipts,
    _executed_steps,
    _explicit_item_debt_fulfillments,
    _player_id,
)


def _assistant_with_authority(*, player_id, steps):
    return SimpleNamespace(
        context_snapshot=json.dumps(
            {
                "interagent_protocol": {"version": 2},
                "turn_authority": {
                    "player_character_id": str(player_id),
                    "action_sequence": {
                        "status": "applied",
                        "steps": steps,
                    },
                },
            }
        )
    )


def test_completed_typed_receipts_are_read_from_authority_snapshot():
    player_id = uuid4()
    target_scene_id = uuid4()
    assistant = _assistant_with_authority(
        player_id=player_id,
        steps=[
            {
                "step_index": 0,
                "action_type": "movement",
                "status": "completed",
                "target_scene_id": str(target_scene_id),
                "observable_outcome": "Кай вышел в коридор.",
            }
        ],
    )
    assert _player_id(assistant) == player_id
    steps = _executed_steps(assistant)
    assert len(steps) == 1
    assert steps[0]["target_scene_id"] == str(target_scene_id)


def test_relationship_reconciler_defaults_to_no_change():
    decision = RelationshipReceiptDecision()
    assert decision.verdict == "no_change"
    assert decision.retract_ids == []


def test_relationship_reconciler_accepts_only_typed_verdicts():
    relationship_id = uuid4()
    decision = RelationshipReceiptDecision(
        verdict="retract",
        retract_ids=[relationship_id],
        reason="Структурированная передача выполнила явное условие долга.",
    )
    assert decision.retract_ids == [relationship_id]


def test_exact_item_receipt_closes_only_item_specific_debt():
    relationship_id = uuid4()
    receipt = {
        "operation": "give",
        "item_name": "Латунный ключ (у Кая)",
        "from_character_id": "kai",
        "to_character_id": "martin",
    }
    row = SimpleNamespace(
        id=relationship_id,
        subject_id="martin",
        object_id="kai",
        relation_type="debt",
        description="Кай должен Мартину вернуть латунный ключ; после возврата долг закрыт.",
    )
    assert _explicit_item_debt_fulfillments(receipt, [row]) == {relationship_id}


@pytest.mark.asyncio
async def test_writer_mode_disables_legacy_relationship_receipt_writer(monkeypatch):
    monkeypatch.setattr(settings, "TE2_SEMANTIC_MODE", "writer")
    # The ownership guard must short-circuit before touching any legacy processor/state.
    assert await _ensure_relationship_receipts(None, None, None, None) == 0


@pytest.mark.asyncio
async def test_job_completes_only_after_receipt_debt_is_closed(db_session, monkeypatch):
    monkeypatch.setattr(settings, "TE2_SEMANTIC_MODE", "legacy")
    campaign = Campaign(name="Receipt completion")
    db_session.add(campaign)
    await db_session.flush()
    user = Turn(campaign_id=campaign.id, role="user", content="Возвращаю ключ.")
    db_session.add(user)
    await db_session.flush()
    player_id, target_id = uuid4(), uuid4()
    authority = _assistant_with_authority(
        player_id=player_id,
        steps=[
            {
                "action_type": "inventory",
                "status": "completed",
                "item_operation": "give",
                "item_name": "латунный ключ",
                "item_result_owner_id": str(target_id),
            }
        ],
    )
    assistant = Turn(
        campaign_id=campaign.id,
        role="assistant",
        content="Ключ возвращён.",
        parent_turn_id=user.id,
        context_snapshot=authority.context_snapshot,
    )
    db_session.add(assistant)
    await db_session.flush()
    job = PostTurnJob(
        campaign_id=campaign.id,
        assistant_turn_id=assistant.id,
        job_type="memory_scribe",
        status="pending",
    )
    debt = RelationshipAssertion(
        campaign_id=campaign.id,
        subject_id=str(player_id),
        object_id=str(target_id),
        relation_type="debt",
        description="Вернуть латунный ключ.",
        is_current=True,
        provenance="manual",
    )
    # A retried job already has its Scribe proposals; receipt reconciliation still has to finish.
    db_session.add(ProposedChange(turn_id=assistant.id, change_type="event", payload="{}"))
    db_session.add_all([job, debt])
    await db_session.commit()

    from app.services.canon_applier import CanonApplier

    apply = CanonApplier.apply

    async def checked_apply(self, *args, **kwargs):
        await db_session.refresh(job)
        assert job.status == "running"
        return await apply(self, *args, **kwargs)

    monkeypatch.setattr(CanonApplier, "apply", checked_apply)
    await PostTurnProcessor(db_session).process_job(UUID(job.id))
    await db_session.refresh(job)
    await db_session.refresh(debt)
    assert job.status == "completed"
    assert debt.is_current is False
