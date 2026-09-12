from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import select

from app.db.repositories.relationship_repo import RelationshipRepository
from app.db.tables import Campaign, Entity, ProposedChange, RelationshipAssertion, Turn
from app.models.relationship import RelationshipCreate
from app.services.truth_engine_relationship_receipts import (
    StructuredRelationshipReceiptProjector,
    _completed_give,
    _item_matches_debt,
    _projection_markers,
)
from app.services.turn_undo_service import TurnUndoService


def _receipt(*, item_id: str, from_id: str, to_id: str, status: str = "completed") -> dict:
    return {
        "event_id": "00000000-0000-4000-8000-000000000099",
        "event_type": "item_transfer",
        "description": "Кай передал Мартину латунный ключ.",
        "payload": {
            "status": status,
            "action_type": "inventory",
            "item_operation": "give",
            "item_id": item_id,
            "item_previous_owner_id": from_id,
            "item_result_owner_id": to_id,
        },
        "effects": [],
    }


def test_item_debt_match_requires_the_transferred_item() -> None:
    assert _item_matches_debt(
        "латунный ключ",
        "Кай должен Мартину вернуть латунный ключ; после возврата долг закрыт.",
    )
    assert not _item_matches_debt(
        "латунный ключ",
        "Кай должен Мартину вернуть старую квитанцию.",
    )


def test_completed_give_rejects_blocked_or_incomplete_receipts() -> None:
    completed = _receipt(
        item_id="00000000-0000-4000-8000-000000000001",
        from_id="00000000-0000-4000-8000-000000000002",
        to_id="00000000-0000-4000-8000-000000000003",
    )
    blocked = {
        **completed,
        "payload": {**completed["payload"], "status": "blocked"},
    }
    missing_owner = {
        **completed,
        "payload": {**completed["payload"], "item_result_owner_id": None},
    }

    assert _completed_give(completed) is completed["payload"]
    assert _completed_give(blocked) is None
    assert _completed_give(missing_owner) is None


def test_projection_marker_is_idempotency_key_independent_of_type_wrapper() -> None:
    marker = "00000000-0000-4000-8000-000000000123"
    proposals = [
        SimpleNamespace(
            change_type="legacy-or-reloaded-wrapper",
            payload={"_te2_receipt_debt_projection_id": marker},
        )
    ]
    assert _projection_markers(proposals) == {marker}


@pytest.mark.asyncio
async def test_te2_give_receipt_closes_item_debt_idempotently_and_undo_restores_it(db_session):
    campaign = Campaign(name="TE2 relationship receipt projection")
    db_session.add(campaign)
    await db_session.flush()

    kai = Entity(
        campaign_id=campaign.id,
        entity_type="character",
        canonical_name="Кай",
    )
    martin = Entity(
        campaign_id=campaign.id,
        entity_type="character",
        canonical_name="Мартин Вэнс",
    )
    key = Entity(
        campaign_id=campaign.id,
        entity_type="item",
        canonical_name="латунный ключ",
    )
    db_session.add_all([kai, martin, key])
    await db_session.flush()

    user = Turn(
        campaign_id=campaign.id,
        role="user",
        content="Я возвращаю Мартину латунный ключ.",
        status="active",
    )
    db_session.add(user)
    await db_session.flush()
    assistant = Turn(
        campaign_id=campaign.id,
        role="assistant",
        content="Кай передаёт Мартину латунный ключ.",
        parent_turn_id=user.id,
        status="active",
        context_snapshot="{}",
    )
    db_session.add(assistant)
    await db_session.flush()

    campaign_id = UUID(campaign.id)
    assistant_id = UUID(assistant.id)
    debt = await RelationshipRepository(db_session).create(
        campaign_id,
        RelationshipCreate(
            subject_id=UUID(martin.id),
            object_id=UUID(kai.id),
            relation_type="debt",
            description=(
                "Кай должен Мартину вернуть латунный ключ; после возврата долг закрыт."
            ),
            reason="Явное условие договора между ними.",
            intensity=-0.4,
            provenance="manual",
            visibility="public",
        ),
    )
    debt_id = debt.id
    receipt = _receipt(item_id=key.id, from_id=kai.id, to_id=martin.id)
    await db_session.commit()

    projector = StructuredRelationshipReceiptProjector(db_session)
    assert await projector.project(campaign_id, assistant_id, [receipt]) == 1
    await db_session.commit()

    rows = list(
        (
            await db_session.execute(
                select(RelationshipAssertion)
                .where(RelationshipAssertion.campaign_id == campaign.id)
                .order_by(RelationshipAssertion.created_at, RelationshipAssertion.id)
            )
        ).scalars().all()
    )
    assert len(rows) == 2
    original = next(row for row in rows if row.id == str(debt_id))
    closed = next(row for row in rows if row.id != str(debt_id))
    assert original.is_current is False
    assert original.superseded_by == closed.id
    assert closed.is_current is True
    assert closed.provenance == "extracted"
    assert closed.source_turn_id == assistant.id
    assert closed.relation_type == "debt"
    assert "долг закрыт" in closed.description.casefold()
    assert "долж" not in closed.description.casefold()
    assert "вернуть" not in closed.description.casefold()

    proposals = list(
        (
            await db_session.execute(
                select(ProposedChange).where(ProposedChange.turn_id == assistant.id)
            )
        ).scalars().all()
    )
    assert len(proposals) == 1
    assert proposals[0].status == "accepted"

    # A retried post-turn writer sees the accepted marker and cannot create another revision.
    assert await projector.project(campaign_id, assistant_id, [receipt]) == 0
    await db_session.commit()
    current_count = len(
        list(
            (
                await db_session.execute(
                    select(RelationshipAssertion).where(
                        RelationshipAssertion.campaign_id == campaign.id,
                        RelationshipAssertion.is_current.is_(True),
                    )
                )
            ).scalars().all()
        )
    )
    assert current_count == 1

    # Replay removes the extracted closed projection and restores the superseded manual debt.
    assert await TurnUndoService(db_session).undo_last_pair(campaign_id) is True
    await db_session.commit()

    after_undo = list(
        (
            await db_session.execute(
                select(RelationshipAssertion).where(
                    RelationshipAssertion.campaign_id == campaign.id
                )
            )
        ).scalars().all()
    )
    assert len(after_undo) == 1
    restored = after_undo[0]
    assert restored.id == str(debt_id)
    assert restored.is_current is True
    assert restored.superseded_by is None
    assert "долж" in restored.description.casefold()
