from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.proposed_change_repo import ProposedChangeRepository
from app.models.proposed_change import ChangeType, ProposalAction, ProposedChangeCreate
from app.models.turn_authority import TurnAuthority
from app.services.canon_applier import CanonApplier


class ResponseMemoryService:
    """Persist approved answers as sourced claims in the same transaction as their publication.

    Exact values survive without a second model summarizing them. Accepted proposals participate in
    the existing undo/replay mechanism; an NPC's answer never becomes objective world canon here.
    """

    def __init__(self, session: AsyncSession):
        self._session = session

    async def publish(self, authority: TurnAuthority, assistant_turn_id: UUID) -> None:
        response = authority.addressed_response
        if not response or not response.speaker_id or not authority.player_character_id:
            return
        changes = [
            ProposedChangeCreate(
                change_type=ChangeType.KNOWLEDGE,
                payload={
                    "recipient_id": str(authority.player_character_id),
                    "source_character_id": str(response.speaker_id),
                    "proposition": f"{response.questions[answer.question_index]} — {answer.words}",
                    "status": "heard",
                    "confidence": 1.0,
                    "_canon": {
                        "owner": "turn_response",
                        "outcome_id": f"response-{answer.question_index}",
                        "kind": "knowledge_transfer",
                        "authority": "character_claim",
                        "evidence": answer.words,
                        "evidence_source": "turn_authority.addressed_response",
                        "durable": True,
                    },
                },
            )
            for answer in response.answers
        ]
        repo = ProposedChangeRepository(self._session)
        existing = await repo.get_for_turn(assistant_turn_id)
        if any(p.payload.get("_canon", {}).get("owner") == "turn_response" for p in existing):
            return
        applier = CanonApplier(self._session)
        for proposal in await repo.create_batch(assistant_turn_id, changes):
            await applier.apply(
                authority.campaign_id, ChangeType.KNOWLEDGE, proposal.payload, assistant_turn_id,
            )
            await repo.resolve(proposal.id, ProposalAction(status="accepted"))

