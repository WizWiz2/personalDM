from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.belief_repo import BeliefRepository
from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.fact_repo import FactRepository
from app.models.fact import FactRead


@dataclass(frozen=True)
class PlayerMemoryView:
    """Player-facing memory projection used by /facts and equivalent UI surfaces."""

    memory_kind: str
    subject: str
    predicate: str
    object_value: str | None
    confidence: float = 1.0


class PlayerMemoryQuery:
    """Combine durable public facts with the protagonist's active beliefs.

    This is a read model, not a second GameApplication. It keeps player-specific projection logic
    out of the application boundary and makes additional player views composable instead of
    encouraging application-subclass proliferation.
    """

    def __init__(self, session: AsyncSession):
        self._session = session
        self._campaigns = CampaignRepository(session)
        self._entities = EntityRepository(session)
        self._facts = FactRepository(session)
        self._beliefs = BeliefRepository(session)

    async def list_active(self, campaign_id: UUID) -> list[FactRead | PlayerMemoryView]:
        facts = await self._facts.list_active(campaign_id)
        result: list[FactRead | PlayerMemoryView] = list(facts)

        campaign = await self._campaigns.get_by_id(campaign_id)
        if not campaign or not campaign.player_character_id:
            return result

        player = await self._entities.get_character(campaign.player_character_id)
        beliefs = await self._beliefs.get_for_character(
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


__all__ = ["PlayerMemoryQuery", "PlayerMemoryView"]
