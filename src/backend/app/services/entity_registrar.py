from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.provider_config_repo import ProviderConfigRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.tables import Entity
from app.models.character import CharacterCreate, CharacterUpdate
from app.models.entity import EntityUpdate
from app.models.proposed_change import ChangeType, ProposedChangeCreate
from app.models.turn import ChatMessage
from app.providers.llm_provider import LLMProvider
from app.services.canon_semantics import evidence_supported
from app.services.role_model_router import ModelRole, RoleModelRouter


class CharacterMention(BaseModel):
    canonical_name: str = Field(min_length=2, max_length=120)
    aliases: list[str] = Field(default_factory=list, max_length=8)
    description: str | None = Field(default=None, max_length=800)
    appearance: str | None = Field(default=None, max_length=800)
    personality: str | None = Field(default=None, max_length=500)
    voice: str | None = Field(default=None, max_length=400)
    role: str | None = Field(default=None, max_length=200)
    evidence: str = Field(min_length=2, max_length=600)
    presence: Literal["present", "departed", "mentioned_only"] = "present"
    importance: Literal["incidental", "supporting", "major"] = "incidental"
    temporary_name: bool = False
    persistent: bool = True


class EntityRegistrationEnvelope(BaseModel):
    characters: list[CharacterMention] = Field(default_factory=list, max_length=12)


@dataclass
class EntityRegistrationResult:
    created_ids: list[UUID] = field(default_factory=list)
    resolved_ids: list[UUID] = field(default_factory=list)
    present_ids: list[UUID] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    telemetry: dict = field(default_factory=dict)

    def gap_proposals(self, scene_id: UUID | None) -> list[ProposedChangeCreate]:
        proposals: list[ProposedChangeCreate] = []
        for index, conflict in enumerate(self.conflicts, start=1):
            proposals.append(
                ProposedChangeCreate(
                    change_type=ChangeType.CANON_GAP,
                    payload={
                        "_validation_error": conflict["error"],
                        "_canon": {
                            "outcome_id": f"presence_conflict_{index}",
                            "kind": "movement",
                            "description": conflict["description"],
                            "evidence": conflict["evidence"],
                            "authority": "public_observation",
                            "scene_id": str(scene_id) if scene_id else None,
                        },
                    },
                )
            )
        return proposals


class EntityRegistrar:
    """Persist concrete NPCs introduced by authoritative narrator prose.

    Registration is intentionally post-turn and transactional with Memory Scribe.
    It never moves an existing character between structured locations. A narrator
    appearance that conflicts with current location becomes a visible canon gap.
    """

    def __init__(self, session: AsyncSession):
        self._session = session
        self._entities = EntityRepository(session)
        self._scenes = SceneRepository(session)
        self._configs = ProviderConfigRepository(session)
        self._router = RoleModelRouter(self._configs)
        self._provider = LLMProvider()

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
        known_lines = [
            f"- {entity.canonical_name} [{entity.entity_type}]"
            + (f"; aliases: {', '.join(entity.aliases)}" if entity.aliases else "")
            for entity in entities
        ]
        participant_names = {
            str(entity.id): entity.canonical_name for entity in entities
        }
        current_names = [
            participant_names.get(str(entity_id), str(entity_id))
            for entity_id in scene.participants
        ]

        selection = await self._router.resolve(
            campaign_id,
            ModelRole.ENTITY_REGISTRAR,
        )
        if selection is None:
            return result

        system_prompt = f"""Ты Entity Registrar русскоязычной настольной RPG.
Из одного авторитетного ответа ДМа выдели персонажей, которых нужно сохранить как сущности.
Верни только JSON по заданной схеме.

ТЕКУЩАЯ СЦЕНА: {scene.title}
ЛОКАЦИЯ: {scene.location_description or 'не задана'}
УЖЕ ПРИСУТСТВУЮТ: {', '.join(current_names) or 'никто не зарегистрирован'}

ИЗВЕСТНЫЕ СУЩНОСТИ:
{chr(10).join(known_lines) or '- нет'}

ПРАВИЛА:
- Возвращай персонажа, если он физически появился, заговорил, напрямую взаимодействовал или повлиял на исход хода.
- Не создавай сущности для толпы, группы, местоимения, безымянного фонового прохожего или человека, которого только упомянули в разговоре.
- Уже известного персонажа можно вернуть, чтобы отметить его присутствие или уход; используй его точное известное имя.
- Не возвращай персонажа игрока, если он уже есть среди известных сущностей.
- canonical_name должно быть устойчивым именем. Для пока безымянного важного NPC допустимо точное временное обозначение вроде «бармен Медного Котла»; тогда temporary_name=true.
- evidence — короткий точный фрагмент ответа ДМа, доказывающий появление, действие, реплику или уход.
- presence=present только если персонаж физически находится в сцене к концу ответа.
- presence=departed только если он явно покинул сцену.
- presence=mentioned_only не добавляет персонажа в сцену.
- persistent=false для чисто фоновой фигуры, которую не нужно помнить.
- Не выдумывай биографию, секреты, мотивацию или внешность сверх текста.
"""

        data = await self._router.generate_json(
            self._provider,
            selection,
            [
                ChatMessage(role="system", content=system_prompt),
                ChatMessage(role="user", content=assistant_content),
            ],
            max_tokens=1100,
            temperature=0.0,
            response_model=EntityRegistrationEnvelope,
        )
        result.telemetry = dict(self._provider.last_telemetry or {})
        envelope = EntityRegistrationEnvelope.model_validate(data)

        index = self._entity_index(entities)
        for mention in envelope.characters:
            if not mention.persistent:
                continue
            if not evidence_supported(mention.evidence, assistant_content):
                continue
            name = self._clean_name(mention.canonical_name)
            if not name:
                continue

            entity = index.get(name.casefold())
            if entity and entity.entity_type != "character":
                result.conflicts.append(
                    {
                        "description": f"{name} описан как персонаж, но имя занято сущностью типа {entity.entity_type}",
                        "evidence": mention.evidence,
                        "error": "Narrator character mention collides with a non-character entity",
                    }
                )
                continue

            if entity:
                character = await self._entities.get_character(entity.id)
                if not character:
                    continue
                await self._enrich_existing(character, mention, source_turn_id, scene_id)
                character_id = entity.id
            else:
                character = await self._entities.create_character(
                    campaign_id,
                    CharacterCreate(
                        canonical_name=name,
                        aliases=self._clean_aliases(mention.aliases, name),
                        description=mention.description or mention.role,
                        appearance=mention.appearance,
                        personality=mention.personality,
                        voice=mention.voice,
                        current_location_id=(
                            scene.location_id
                            if mention.presence == "present"
                            else None
                        ),
                        custom_fields={
                            "registrar": "entity_registrar",
                            "source_turn_id": str(source_turn_id),
                            "first_seen_scene_id": str(scene_id),
                            "role": mention.role,
                            "importance": mention.importance,
                            "temporary_name": mention.temporary_name,
                        },
                    ),
                )
                db_entity = await self._session.get(Entity, str(character.id))
                if db_entity:
                    db_entity.provenance = "narrator_extracted"
                character_id = character.id
                result.created_ids.append(character_id)
                index[name.casefold()] = character
                for alias in character.aliases:
                    index[alias.casefold()] = character

            result.resolved_ids.append(character_id)
            if mention.presence == "present":
                try:
                    await self._scenes.add_participant(scene_id, character_id)
                except ValueError as exc:
                    current = await self._entities.get_character(character_id)
                    result.conflicts.append(
                        {
                            "description": (
                                f"{name} появился в сцене «{scene.title}», хотя его "
                                f"структурная локация — {current.current_location_id if current else 'неизвестна'}"
                            ),
                            "evidence": mention.evidence,
                            "error": str(exc),
                        }
                    )
                    continue
                result.present_ids.append(character_id)
            elif mention.presence == "departed":
                await self._scenes.remove_participant(scene_id, character_id)

        await self._session.flush()
        return result

    async def _enrich_existing(
        self,
        character,
        mention: CharacterMention,
        source_turn_id: UUID,
        scene_id: UUID,
    ) -> None:
        aliases = list(dict.fromkeys([
            *character.aliases,
            *self._clean_aliases(mention.aliases, character.canonical_name),
        ]))
        custom_fields = dict(character.custom_fields or {})
        custom_fields.setdefault("registrar", "entity_registrar")
        custom_fields.setdefault("source_turn_id", str(source_turn_id))
        custom_fields.setdefault("first_seen_scene_id", str(scene_id))
        if mention.role:
            custom_fields.setdefault("role", mention.role)
        custom_fields.setdefault("importance", mention.importance)
        if mention.temporary_name:
            custom_fields.setdefault("temporary_name", True)

        await self._entities.update(
            character.id,
            EntityUpdate(
                aliases=aliases,
                description=character.description or mention.description or mention.role,
                custom_fields=custom_fields,
            ),
        )
        character_updates = {}
        for key in ("appearance", "personality", "voice"):
            if not getattr(character, key) and getattr(mention, key):
                character_updates[key] = getattr(mention, key)
        if character_updates:
            await self._entities.update_character(
                character.id,
                CharacterUpdate(**character_updates),
            )

    @staticmethod
    def _entity_index(entities) -> dict[str, object]:
        result = {}
        for entity in entities:
            result[entity.canonical_name.casefold()] = entity
            for alias in entity.aliases:
                result[alias.casefold()] = entity
        return result

    @staticmethod
    def _clean_name(value: str) -> str | None:
        value = " ".join(value.split()).strip(" .,:;!?—–-")
        if len(value) < 2 or len(value) > 120:
            return None
        if value.casefold() in {"кто-то", "некто", "человек", "толпа", "они", "он", "она"}:
            return None
        return value

    @staticmethod
    def _clean_aliases(values: list[str], canonical_name: str) -> list[str]:
        result = []
        canonical = canonical_name.casefold()
        for value in values:
            clean = " ".join(str(value).split()).strip(" .,:;!?—–-")
            if clean and clean.casefold() != canonical and clean not in result:
                result.append(clean[:120])
        return result[:8]
