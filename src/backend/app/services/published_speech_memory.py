"""Sourced conversation evidence committed atomically with publication, independent of Scribe."""
from app.db.repositories.proposed_change_repo import ProposedChangeRepository
from app.models.proposed_change import ChangeType, ProposalAction, ProposedChangeCreate
from app.services.canon_applier import CanonApplier


async def remember_published_speech(session, authority, assistant, contract, beat, present_ids):
    if not authority.player_character_id:
        return
    player = authority.player_character_id
    proposals = []

    def heard(listener, speaker, text):
        proposals.append(ProposedChangeCreate(change_type=ChangeType.KNOWLEDGE, payload={
            "recipient_id": str(listener), "source_character_id": str(speaker),
            "proposition": text, "status": "known", "confidence": 1.0,
            "_canon": {"owner": "turn_response", "authority": "character_claim", "evidence": text},
        }))

    # Hearing a claim is durable. It neither proves the claim nor executes the requested act.
    # Without a typed speech timestamp, do not assign pre-travel words to a new
    # destination cast. Stationary dialogue has an unambiguous listener frame.
    stationary = not any(step.get("action_type") == "movement" and step.get("status") == "completed"
                         for step in authority.executed_steps())
    for text in (contract.get("spoken_contributions") or []) if stationary else []:
        if text and text in authority.player_input:
            for listener in present_ids:
                if str(listener) != str(player):
                    heard(listener, player, text)
    # The granted beat certifies its speaker and exact span, with no second inference call.
    if (beat and beat.kind in {"speech", "refusal"} and authority.beat_owner_id
            and beat.cast_id == str(authority.beat_owner_id) and beat.evidence in assistant.content):
        heard(player, authority.beat_owner_id, beat.evidence)
    repo = ProposedChangeRepository(session)
    for proposal in await repo.create_batch(assistant.id, proposals):
        await CanonApplier(session).apply(authority.campaign_id, ChangeType.KNOWLEDGE,
                                         proposal.payload, assistant.id)
        await repo.resolve(proposal.id, ProposalAction(status="accepted"))
