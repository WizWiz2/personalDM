from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from app.application.game_application import GameApplication as BaseGameApplication
from app.db.repositories.belief_repo import BeliefRepository


@dataclass(frozen=True)
class PlayerMemoryView:
    """Minimal common shape consumed by the current CLI /facts renderer."""

    memory_kind: str
    subject: str
    predicate: str
    object_value: str | None
    confidence: float = 1.0


class GameApplication(BaseGameApplication):
    """Player-facing application boundary with a truthful combined memory view."""

    async def list_active_facts(self, campaign_id: UUID) -> list:
        facts = await self._facts.list_active(campaign_id)
        result: list = list(facts)

        campaign = await self._campaigns.get_by_id(campaign_id)
        if not campaign or not campaign.player_character_id:
            return result
        player = await self._entities.get_character(campaign.player_character_id)
        beliefs = await BeliefRepository(self._session).get_for_character(
            campaign.player_character_id,
            active_only=True,
        )
        player_name = player.canonical_name if player else "Герой"
        known = {
            " ".join(
                f"{fact.subject} {fact.predicate} {fact.object_value or ''}".casefold().split()
            )
            for fact in facts
        }
        for belief in beliefs:
            proposition = " ".join(belief.proposition.split())
            if not proposition or proposition.casefold() in known:
                continue
            result.append(
                PlayerMemoryView(
                    memory_kind="belief",
                    subject=player_name,
                    predicate="знает:",
                    object_value=proposition,
                    confidence=belief.confidence,
                )
            )
        return result


__all__ = ["GameApplication", "PlayerMemoryView"]
