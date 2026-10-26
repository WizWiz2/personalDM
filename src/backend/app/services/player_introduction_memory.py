from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.scene_repo import SceneRepository
from app.db.tables import Campaign, Entity
from app.models.proposed_change import ChangeType, ProposedChangeCreate


async def introduction_knowledge(session: AsyncSession, campaign_id, assistant, user_turn, snapshot):
    """Remember an actual introduction as a heard claim for its present addressee.

    The quoted input is immutable evidence, not objective proof of the claimed identity.
    Knowledge is tied to the assistant turn and follows normal undo/replay semantics.
    """
    contract = ((snapshot.get("turn_planner") or {}).get("telemetry") or {}).get("intent_contract") or {}
    evidence = contract.get("spoken_identity_evidence")
    authority = snapshot.get("turn_authority") or {}
    listener_id = authority.get("beat_owner_id")
    campaign = await session.get(Campaign, str(campaign_id))
    if not (evidence and evidence in user_turn.content and listener_id
            and campaign and campaign.player_character_id and assistant.scene_id):
        return []
    participants = await SceneRepository(session).get_participants(assistant.scene_id)
    if str(listener_id) not in {str(value) for value in participants}:
        return []
    player = await session.get(Entity, campaign.player_character_id)
    if not player or str(listener_id) == player.id:
        return []
    return [ProposedChangeCreate(change_type=ChangeType.KNOWLEDGE, payload={
        "recipient_id": str(listener_id), "source_character_id": player.id,
        "proposition": f"Представление {player.canonical_name}: «{evidence}».",
        "status": "known", "confidence": 1.0,
    })]
