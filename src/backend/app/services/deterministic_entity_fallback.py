from __future__ import annotations

import re
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.scene_repo import SceneRepository
from app.models.character import CharacterCreate
from app.services.entity_registrar import EntityRegistrationResult


class DeterministicEntityFallback:
    """Register a small set of explicit acting role-NPCs without an LLM.

    This fallback is intentionally conservative. It only activates when a concrete
    role label occurs in a sentence with an observable action or a speech verb.
    Crowds and merely mentioned people are ignored.
    """

    ROLE_PATTERN = re.compile(
        r"\b((?:бармен|трактирщик|трактирщица|хозяин таверны|хозяйка таверны|"
        r"стражник|стражница|купец|торговка|официант|официантка|слуга|служанка|"
        r"bartender|innkeeper|guard|merchant|waiter|waitress)"
        r"(?:\s+[А-ЯЁA-Z][а-яёa-z-]+){0,2})\b",
        re.IGNORECASE,
    )
    ACTION_PATTERN = re.compile(
        r"\b(говорит|спрашивает|отвечает|кивает|смотрит|улыбается|клад[её]т|"
        r"ставит|бер[её]т|наклоняется|подходит|уходит|произносит|шепчет|"
        r"says|asks|answers|nods|looks|smiles|puts|takes|approaches|leaves)\b",
        re.IGNORECASE,
    )

    def __init__(self, session: AsyncSession):
        self._session = session
        self._entities = EntityRepository(session)
        self._scenes = SceneRepository(session)

    async def register_from_turn(
        self,
        campaign_id: UUID,
        scene_id: UUID | None,
        source_turn_id: UUID,
        assistant_content: str,
    ) -> EntityRegistrationResult:
        result = EntityRegistrationResult()
        if not scene_id or not assistant_content.strip():
            return result
        scene = await self._scenes.get_by_id(scene_id)
        if not scene:
            return result

        entities = await self._entities.list_by_campaign(campaign_id)
        index = {entity.canonical_name.casefold(): entity for entity in entities}
        for entity in entities:
            for alias in entity.aliases:
                index.setdefault(alias.casefold(), entity)

        for sentence in self._sentences(assistant_content):
            if not self.ACTION_PATTERN.search(sentence):
                continue
            for match in self.ROLE_PATTERN.finditer(sentence):
                name = self._canonical_name(match.group(1))
                if not name:
                    continue
                entity = index.get(name.casefold())
                if entity and entity.entity_type != "character":
                    continue
                if entity is None:
                    character = await self._entities.create_character(
                        campaign_id,
                        CharacterCreate(
                            canonical_name=name,
                            description=f"Временно зарегистрированный NPC: {name}.",
                            current_location_id=scene.location_id,
                            custom_fields={
                                "registrar": "deterministic_role_fallback",
                                "source_turn_id": str(source_turn_id),
                                "first_seen_scene_id": str(scene_id),
                                "temporary_name": True,
                                "importance": "supporting",
                            },
                        ),
                    )
                    entity = character
                    index[name.casefold()] = entity
                    result.created_ids.append(entity.id)
                result.resolved_ids.append(entity.id)
                try:
                    await self._scenes.add_participant(scene_id, entity.id)
                except ValueError as exc:
                    result.conflicts.append(
                        {
                            "description": f"{name} упомянут как присутствующий, но находится в другой локации",
                            "evidence": sentence[:600],
                            "error": str(exc),
                        }
                    )
                    continue
                result.present_ids.append(entity.id)
        await self._session.flush()
        return result

    @staticmethod
    def _sentences(text: str) -> list[str]:
        return [
            part.strip()
            for part in re.split(r"(?<=[.!?])\s+|\n+", text)
            if part.strip()
        ]

    @staticmethod
    def _canonical_name(value: str) -> str | None:
        clean = " ".join(value.split()).strip(" .,:;!?—–-")
        if not clean:
            return None
        words = clean.split()
        normalized = " ".join(
            word if any(char.isupper() for char in word[1:]) else word.capitalize()
            for word in words
        )
        return normalized[:120]
