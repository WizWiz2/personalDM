from __future__ import annotations

import re
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.scene_repo import SceneRepository
from app.models.proposed_change import ChangeType, ProposedChangeCreate


class ProposalPresenceResolver:
    """Enrich canon proposals using the authoritative participant set of one scene.

    Scribe may omit event participant IDs even when the authoritative prose names
    a registered NPC. This resolver only adds characters that are already present in
    the structured scene and explicitly named in the event description or evidence.
    It never invents witnesses and never moves an entity.

    The assistant turn's structured scene is also authoritative for the player's final
    physical position. A post-turn Scribe proposal may describe that position, but it
    may not move the protagonist back to an intermediate location after a structured
    transition or compound action sequence has already completed.
    """

    def __init__(self, session: AsyncSession):
        self._session = session
        self._campaigns = CampaignRepository(session)
        self._entities = EntityRepository(session)
        self._scenes = SceneRepository(session)

    async def enrich(
        self,
        campaign_id: UUID,
        scene_id: UUID | None,
        proposals: list[ProposedChangeCreate],
    ) -> list[ProposedChangeCreate]:
        if not scene_id or not proposals:
            return proposals
        scene = await self._scenes.get_by_id(scene_id)
        if not scene:
            return proposals
        campaign = await self._campaigns.get_by_id(campaign_id)

        aliases: dict[str, str] = {}
        for participant_id in scene.participants:
            entity = await self._entities.get_by_id(participant_id)
            if not entity or entity.campaign_id != campaign_id:
                continue
            for name in (entity.canonical_name, *entity.aliases):
                normalized = self._normalize(name)
                if len(normalized) >= 3:
                    aliases[normalized] = str(entity.id)

        for proposal in proposals:
            payload = proposal.payload
            if (
                proposal.change_type == ChangeType.MOVEMENT
                and campaign
                and campaign.player_character_id
                and str(payload.get("character_id") or "")
                == str(campaign.player_character_id)
                and scene.location_id
                and payload.get("location_id")
                and str(payload.get("location_id")) != str(scene.location_id)
            ):
                payload["_validation_error"] = (
                    "Player movement conflicts with the authoritative assistant scene "
                    f"location {scene.location_id}; post-turn memory cannot override "
                    "a structured scene transition."
                )

            if proposal.change_type != ChangeType.EVENT:
                continue
            canon = payload.get("_canon")
            evidence = canon.get("evidence") if isinstance(canon, dict) else ""
            text = self._normalize(
                " ".join(
                    value
                    for value in (
                        payload.get("description"),
                        evidence,
                    )
                    if isinstance(value, str)
                )
            )
            participant_ids = [
                str(value)
                for value in payload.get("participant_ids", [])
                if value
            ]
            for alias, entity_id in aliases.items():
                if self._contains_name(text, alias):
                    participant_ids.append(entity_id)
            payload["participant_ids"] = list(dict.fromkeys(participant_ids))
            if not payload.get("location_id") and scene.location_id:
                payload["location_id"] = str(scene.location_id)
        return proposals

    @staticmethod
    def _normalize(value: object) -> str:
        return " ".join(str(value or "").casefold().split())

    @staticmethod
    def _contains_name(text: str, name: str) -> bool:
        # Word boundaries based on \w work for Cyrillic in Python and prevent a short
        # alias from matching inside an unrelated word.
        return bool(re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text))
