from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.scene_repo import SceneRepository
from app.models.character import CharacterCreate
from app.models.turn_authority import TurnAuthority


@dataclass(frozen=True)
class MaterializedTurnOutcome:
    introduced_character_ids: tuple[UUID, ...] = ()


class TurnOutcomeMaterializer:
    """Apply structured turn outcomes that must not be re-inferred from narrator prose."""

    def __init__(self, session: AsyncSession):
        self._session = session
        self._entities = EntityRepository(session)
        self._scenes = SceneRepository(session)

    async def materialize(
        self,
        authority: TurnAuthority,
        *,
        source_turn_id: UUID,
    ) -> MaterializedTurnOutcome:
        if not authority.allowed_new_npcs:
            return MaterializedTurnOutcome()
        if not authority.target_scene_id:
            raise ValueError("Planned NPC introduction has no authoritative target scene")

        known = await self._entities.list_by_campaign(
            authority.campaign_id,
            entity_type="character",
        )
        known_names: set[str] = set()
        for entity in known:
            known_names.add(self._key(entity.canonical_name))
            known_names.update(self._key(alias) for alias in entity.aliases)

        created_ids: list[UUID] = []
        for introduction in authority.allowed_new_npcs:
            key = self._key(introduction.canonical_name)
            if key in known_names:
                raise ValueError(
                    "Cannot materialize planned new NPC because that identity already exists: "
                    f"{introduction.canonical_name}"
                )
            character = await self._entities.create_character(
                authority.campaign_id,
                CharacterCreate(
                    canonical_name=introduction.canonical_name,
                    description=introduction.description or introduction.role,
                    appearance=introduction.appearance,
                    voice=introduction.voice,
                    custom_fields={
                        "introduced_by": "turn_authority",
                        "introduction_turn_id": str(source_turn_id),
                        "introduction_reason": introduction.reason,
                        "temporary_name": introduction.temporary_name,
                    },
                ),
            )
            await self._scenes.add_participant(
                authority.target_scene_id,
                character.id,
                allow_movement=True,
            )
            created_ids.append(character.id)
            known_names.add(key)

        await self._session.flush()
        return MaterializedTurnOutcome(tuple(created_ids))

    @staticmethod
    def _key(value: object) -> str:
        return " ".join(str(value or "").casefold().split())


__all__ = ["MaterializedTurnOutcome", "TurnOutcomeMaterializer"]
