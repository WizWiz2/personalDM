from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, ValidationError, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.provider_config_repo import ProviderConfigRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.tables import Campaign, Entity
from app.models.character import CharacterCreate, CharacterUpdate
from app.models.entity import EntityUpdate
from app.models.proposed_change import ChangeType, ProposedChangeCreate
from app.models.turn import ChatMessage
from app.providers.llm_provider import LLMProvider, LLMProviderError
from app.services.canon_semantics import evidence_supported
from app.services.entity_identity import identity_key, resolve_character_candidates
from app.services.name_identity_contract import (
    description_used_as_identity_name,
    is_usable_short_designation,
)
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
    personal_name_evidence: str | None = Field(default=None, max_length=500)
    persistent: bool = True


class EntityRegistrationEnvelope(BaseModel):
    characters: list[CharacterMention] = Field(default_factory=list, max_length=12)


class PersonalNameRevealDecision(BaseModel):
    """Semantic confirmation for a named reveal misclassified as temporary by the registrar."""

    is_explicit: bool = False
    evidence: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def explicit_reveal_needs_evidence(self):
        if self.is_explicit and not (self.evidence or "").strip():
            raise ValueError("is_explicit=true requires a verbatim self-identification evidence quote")
        return self


class PersonalNameBindingDecision(PersonalNameRevealDecision):
    """Identify the speaker among several existing temporary scene participants."""

    entity_id: UUID | None

    @model_validator(mode="after")
    def explicit_binding_needs_id(self):
        if self.is_explicit and self.entity_id is None:
            raise ValueError("is_explicit=true requires the existing speaker's entity_id")
        return self


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
    Model or schema failure is fail-open so the established Scribe path still runs.
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
        *,
        promotion_only: bool = False,
    ) -> EntityRegistrationResult:
        result = EntityRegistrationResult()
        if not scene_id or not assistant_content.strip():
            return result

        scene = await self._scenes.get_by_id(scene_id)
        campaign = await self._session.get(Campaign, str(campaign_id))
        if not scene or not campaign:
            return result

        entities = await self._entities.list_by_campaign(campaign_id)
        character_entities = [
            entity for entity in entities if entity.entity_type == "character"
        ]
        character_profiles = {
            str(entity.id): await self._entities.get_character(entity.id)
            for entity in character_entities
        }
        known_lines = [
            f"- {entity.canonical_name} [{entity.entity_type}]"
            + (f"; aliases: {', '.join(entity.aliases)}" if entity.aliases else "")
            + (f"; status: {entity.status}" if entity.status != "active" else "")
            + (
                "; identity card: " + json.dumps(
                    self._identity_card(character_profiles[str(entity.id)]), ensure_ascii=False,
                )
                if entity.id in scene.participants and character_profiles.get(str(entity.id))
                else ""
            )
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
- Персонаж со status=dead/destroyed не может снова физически появиться только из-за текста Narrator. Для исторического упоминания используй mentioned_only.
- Не возвращай персонажа игрока, если он уже есть среди известных сущностей.
- canonical_name должно быть коротким личным именем ИЛИ коротким ролевым обозначением (без описательных придаточных), реально присутствующим в тексте ответа. Не копируй description в canonical_name.
- Не придумывай canonical_name, которого нет в тексте ответа. Запрещены синтетические ярлыки вроде «Городской Диктатор» или «Безымянный собеседник», если Narrator буквально так персонажа не назвал.
- Для пока безымянного важного NPC допустимо точное временное обозначение вроде «бармен Медного Котла»; тогда temporary_name=true.
- Если временный NPC позже назван по имени, верни новое имя, ту же role и temporary_name=false: движок сам повысит временную идентичность до постоянной.
- Если несколько NPC имеют одну роль, различай их по identity card: внешности и описанию.
  Верни каждого представившегося персонажа отдельно с личным именем и точной цитатой.
- Если персонаж прямо сам сообщает личное имя (например, «Меня зовут Иван»), заполни
  personal_name_evidence точной цитатой из ответа ДМа. Такое evidence важнее случайного
  значения temporary_name: движок всё равно проверит цитату и однозначность сцены перед promotion.
- evidence — короткий точный фрагмент ответа ДМа, доказывающий появление, действие, реплику или уход.
- presence=present только если персонаж физически находится в сцене к концу ответа.
- presence=departed только если он явно покинул сцену.
- presence=mentioned_only не добавляет персонажа в сцену.
- persistent=false для чисто фоновой фигуры, которую не нужно помнить.
- Не выдумывай биографию, секреты, мотивацию или внешность сверх текста.
"""

        try:
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
        except (LLMProviderError, ValidationError) as exc:
            result.telemetry = {
                **dict(self._provider.last_telemetry or {}),
                "status": "failed_open",
                "error": str(exc)[:2000],
            }
            return result

        character_locations: dict[UUID, UUID | None] = {}
        for entity in character_entities:
            character = character_profiles[str(entity.id)]
            if character:
                character_locations[UUID(str(entity.id))] = character.current_location_id

        index = self._entity_index(entities)
        for mention in envelope.characters:
            if not mention.persistent:
                continue
            if not evidence_supported(mention.evidence, assistant_content):
                continue
            name = self._clean_name(mention.canonical_name)
            if not name:
                continue
            # Never persist description/blurb text as the durable entity identity label.
            if description_used_as_identity_name(
                name, role=mention.role, description=mention.description
            ):
                role = self._clean_name(mention.role or "")
                if is_usable_short_designation(role):
                    name = role
                    mention = mention.model_copy(
                        update={"canonical_name": name, "temporary_name": True}
                    )
                else:
                    continue

            entity = index.get(identity_key(name))
            matched_contextually = False
            confirmed_evidence = None
            if entity is None:
                # New identities and named reveals must be grounded in the published prose itself.
                # Evidence support alone is insufficient because the registrar model can quote a
                # real sentence while inventing a canonical_name in another JSON field.
                if not self._name_supported_by_text(name, assistant_content):
                    continue
                contextual = resolve_character_candidates(
                    character_entities,
                    proposed_name=name,
                    proposed_role=mention.role,
                    temporary_name=mention.temporary_name,
                    target_location_id=scene.location_id,
                    character_locations=character_locations,
                )
                unique_contextual = {
                    UUID(str(candidate.id)): candidate for candidate in contextual
                }
                if len(unique_contextual) != 1 and mention.presence != "mentioned_only":
                    candidates = [
                        candidate for candidate in character_entities
                        if self._is_temporary_identity(candidate)
                        and candidate.id in scene.participants
                        and character_locations.get(candidate.id) == scene.location_id
                    ]
                    if candidates:
                        bound = await self._bind_personal_name_reveal(
                            selection, assistant_content, mention,
                            [character_profiles[str(candidate.id)] for candidate in candidates],
                        )
                        if bound:
                            bound_id, confirmed_evidence = bound
                            unique_contextual = {
                                candidate.id: candidate for candidate in candidates
                                if candidate.id == bound_id
                            }
                        elif promotion_only and not unique_contextual:
                            result.conflicts.append({
                                "description": f"Name {name} could not be bound to a scene participant.",
                                "evidence": mention.evidence,
                                "error": "Unconfirmed contextual identity binding",
                            })
                            continue
                if len(unique_contextual) > 1:
                    names = ", ".join(
                        sorted(
                            candidate.canonical_name
                            for candidate in unique_contextual.values()
                        )
                    )
                    result.conflicts.append(
                        {
                            "description": (
                                f"Временное имя {name} неоднозначно совпадает с известными "
                                f"персонажами в этой локации: {names}"
                            ),
                            "evidence": mention.evidence,
                            "error": "Ambiguous role/location entity identity",
                        }
                    )
                    continue
                if unique_contextual:
                    entity = next(iter(unique_contextual.values()))
                    matched_contextually = True

            if entity and str(entity.id) == campaign.player_character_id:
                # Mixed-script variants such as Эйdan/Rэт resolve to the real player identity here.
                continue
            if entity and entity.entity_type != "character":
                result.conflicts.append(
                    {
                        "description": f"{name} описан как персонаж, но имя занято сущностью типа {entity.entity_type}",
                        "evidence": mention.evidence,
                        "error": "Narrator character mention collides with a non-character entity",
                    }
                )
                continue

            if entity and entity.status in {"dead", "destroyed"}:
                if mention.presence != "mentioned_only":
                    result.conflicts.append(
                        {
                            "description": (
                                f"{entity.canonical_name} имеет статус {entity.status} и не может "
                                f"быть материализован как присутствующий персонаж без отдельного "
                                f"авторитетного изменения статуса"
                            ),
                            "evidence": mention.evidence,
                            "error": (
                                f"Character status {entity.status} cannot be materialized "
                                "by narrator extraction"
                            ),
                        }
                    )
                result.resolved_ids.append(entity.id)
                continue

            if entity:
                if (
                    matched_contextually
                    and self._is_temporary_identity(entity)
                    and identity_key(name) != identity_key(entity.canonical_name)
                ):
                    # Role/location similarity proposes a candidate, not an identity binding.
                    # Neither an extractor's temporary flag nor a real but unrelated quotation
                    # authorizes merging a discourse referent into this physical participant.
                    if not confirmed_evidence and mention.presence != "mentioned_only":
                        confirmed_evidence = await self._confirm_personal_name_reveal(
                            selection, assistant_content, entity.canonical_name, mention,
                        )
                    if not confirmed_evidence:
                        result.conflicts.append({
                            "description": f"Name {name} is not bound to {entity.canonical_name}.",
                            "evidence": mention.evidence,
                            "error": "Unconfirmed contextual identity binding",
                        })
                        # Do not enrich aliases/profile or mark the candidate resolved either.
                        continue
                    old_name = entity.canonical_name
                    promoted = await self._promote_temporary_identity(
                        entity,
                        new_name=name,
                        mention=mention,
                        source_turn_id=source_turn_id,
                        binding_evidence=confirmed_evidence,
                    )
                    if promoted is not None:
                        entity = promoted
                        index[identity_key(old_name)] = entity
                        index[identity_key(name)] = entity
                        for alias in entity.aliases:
                            index[identity_key(alias)] = entity
                        # Later mentions in this same answer must see the promoted identity.
                        character_entities = [
                            entity if candidate.id == entity.id else candidate
                            for candidate in character_entities
                        ]

                character = await self._entities.get_character(entity.id)
                if not character:
                    continue
                await self._enrich_existing(character, mention, source_turn_id, scene_id)
                character_id = entity.id
            else:
                if promotion_only:
                    # This mode is used after TurnAuthority has already materialized all
                    # authorized first appearances. It may reconcile a published name with an
                    # existing temporary identity, but it is never allowed to create a new one.
                    continue
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
                index[identity_key(name)] = character
                for alias in character.aliases:
                    index[identity_key(alias)] = character
                character_entities.append(character)
                character_locations[character.id] = character.current_location_id

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

    @staticmethod
    def _identity_card(character) -> dict:
        return {
            "entity_id": str(character.id),
            "canonical_name": character.canonical_name,
            "aliases": character.aliases,
            "role": (character.custom_fields or {}).get("role"),
            "description": character.description,
            "appearance": getattr(character, "appearance", None),
        }

    async def _bind_personal_name_reveal(self, selection, text, mention, candidates):
        """Choose an existing ID using distinguishing evidence, never order or role alone."""
        try:
            data = await self._router.generate_json(
                self._provider, selection,
                [
                    ChatMessage(role="system", content=(
                        "Resolve a published self-identification to ONE existing NPC identity card. "
                        "Several people may share a role. Use distinguishing appearance, description "
                        "and speaker context, never list order or shared role alone. A name embedded "
                        "in a card is only a hint: the published text must explicitly establish it. "
                        "Return entity_id from the supplied cards and is_explicit=true only when "
                        "the named speaker is unambiguous. evidence must be a verbatim excerpt "
                        "containing the proposed name. A third person's name, a job/title, or an "
                        "ambiguous speaker is not a reveal: return entity_id=null, is_explicit=false."
                    )),
                    ChatMessage(role="user", content=json.dumps({
                        "mention": mention.model_dump(mode="json"),
                        "existing_candidates": [self._identity_card(c) for c in candidates],
                        "published_text": text,
                    }, ensure_ascii=False)),
                ],
                max_tokens=350, temperature=0.0, response_model=PersonalNameBindingDecision,
            )
            decision = PersonalNameBindingDecision.model_validate(data)
            evidence = (decision.evidence or "").strip()
            if (
                decision.is_explicit
                and decision.entity_id in {c.id for c in candidates}
                and evidence and evidence in text
                and self._name_supported_by_text(mention.canonical_name, evidence)
            ):
                return decision.entity_id, evidence
        except (LLMProviderError, ValidationError, ValueError, TypeError):
            pass
        return None

    async def _confirm_personal_name_reveal(
        self,
        selection,
        assistant_content: str,
        previous_identity: str,
        mention: CharacterMention,
    ) -> str | None:
        """Confirm a self-identification semantically, without parsing prose lexically."""
        try:
            data = await self._router.generate_json(
                self._provider,
                selection,
                [
                    ChatMessage(
                        role="system",
                        content=(
                            "Ты semantic identity verifier. Определи только, устанавливает ли "
                            "приведённый фрагмент личное имя уже присутствующего временного "
                            "персонажа. Не считай роль, должность, обращение или имя, названное "
                            "третьим лицом, self-identification. Верни is_explicit=true только "
                            "при прямом сообщении самим персонажем; evidence должна быть точной "
                            "цитатой из текста. Не придумывай и не исправляй цитату."
                        ),
                    ),
                    ChatMessage(
                        role="user",
                        content=(
                            f"ПРЕЖНЯЯ ВРЕМЕННАЯ ИДЕНТИЧНОСТЬ: {previous_identity}\n"
                            f"УПОМИНАНИЕ REGISTRAR: {mention.model_dump_json()}\n"
                            f"ОПУБЛИКОВАННЫЙ ОТВЕТ ДМА:\n{assistant_content}"
                        ),
                    ),
                ],
                max_tokens=250,
                temperature=0.0,
                response_model=PersonalNameRevealDecision,
            )
            decision = PersonalNameRevealDecision.model_validate(data)
            evidence = (decision.evidence or "").strip()
            # Extractors sometimes wrap the excerpt in quotation marks of their own. Those
            # delimiters are not part of the cited span: the source may continue with a comma
            # before its closing quote. Match the literal inner span, preserving its punctuation.
            quote_pairs = {'"': '"', "'": "'", "«": "»", "“": "”", "‘": "’"}
            if len(evidence) > 1 and quote_pairs.get(evidence[0]) == evidence[-1]:
                evidence = evidence[1:-1].strip()
            if (
                decision.is_explicit and evidence
                and evidence in assistant_content
                and self._name_supported_by_text(mention.canonical_name, evidence)
                and self._name_supported_by_text(mention.canonical_name, assistant_content)
            ):
                return evidence
        except (LLMProviderError, ValidationError, ValueError, TypeError):
            return None
        return None

    async def _promote_temporary_identity(
        self,
        entity,
        *,
        new_name: str,
        mention: CharacterMention,
        source_turn_id: UUID,
        binding_evidence: str,
    ):
        old_name = entity.canonical_name
        aliases = self._clean_aliases(
            [old_name, *entity.aliases, *mention.aliases],
            new_name,
        )
        custom_fields = dict(entity.custom_fields or {})
        custom_fields["temporary_name"] = False
        custom_fields.setdefault("identity_promoted_from", old_name)
        custom_fields["identity_promoted_turn_id"] = str(source_turn_id)
        custom_fields["identity_binding"] = {
            "entity_id": str(entity.id), "previous_designation": old_name,
            "personal_name": new_name, "evidence": binding_evidence,
            "source_turn_id": str(source_turn_id), "source": "published_self_identification",
        }
        if mention.role:
            custom_fields["role"] = mention.role

        return await self._entities.update(
            entity.id,
            EntityUpdate(
                canonical_name=new_name,
                aliases=aliases,
                custom_fields=custom_fields,
            ),
        )

    async def _enrich_existing(
        self,
        character,
        mention: CharacterMention,
        source_turn_id: UUID,
        scene_id: UUID,
    ) -> None:
        aliases = list(
            dict.fromkeys(
                [
                    *character.aliases,
                    *self._clean_aliases(mention.aliases, character.canonical_name),
                ]
            )
        )
        custom_fields = dict(character.custom_fields or {})
        custom_fields.setdefault("registrar", "entity_registrar")
        custom_fields.setdefault("source_turn_id", str(source_turn_id))
        custom_fields.setdefault("first_seen_scene_id", str(scene_id))
        if mention.role:
            custom_fields.setdefault("role", mention.role)
        custom_fields.setdefault("importance", mention.importance)
        if mention.temporary_name:
            custom_fields.setdefault("temporary_name", True)

        entity_updates = {}
        if aliases != character.aliases:
            entity_updates["aliases"] = aliases
        description = character.description or mention.description or mention.role
        if description != character.description:
            entity_updates["description"] = description
        if custom_fields != (character.custom_fields or {}):
            entity_updates["custom_fields"] = custom_fields
        if entity_updates:
            await self._entities.update(
                character.id,
                EntityUpdate(**entity_updates),
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
            result[identity_key(entity.canonical_name)] = entity
            for alias in entity.aliases:
                result[identity_key(alias)] = entity
        return result

    @staticmethod
    def _is_temporary_identity(entity) -> bool:
        custom_fields = getattr(entity, "custom_fields", None) or {}
        return bool(
            isinstance(custom_fields, dict)
            and custom_fields.get("temporary_name")
        )

    @staticmethod
    def _name_supported_by_text(name: str, assistant_content: str) -> bool:
        name_key = identity_key(name)
        text_key = identity_key(assistant_content)
        if not name_key or not text_key:
            return False
        return f" {name_key} " in f" {text_key} "

    @staticmethod
    def _clean_name(value: str) -> str | None:
        value = " ".join(value.split()).strip(" .,:;!?—–-")
        if len(value) < 2 or len(value) > 120:
            return None
        if value.casefold() in {
            "кто-то",
            "некто",
            "человек",
            "толпа",
            "они",
            "он",
            "она",
            "безымянный собеседник",
            "неизвестный собеседник",
            "неизвестный npc",
            "безымянный npc",
        }:
            return None
        return value

    @staticmethod
    def _clean_aliases(values: list[str], canonical_name: str) -> list[str]:
        result = []
        seen = {identity_key(canonical_name)}
        for value in values:
            clean = " ".join(str(value).split()).strip(" .,:;!?—–-")
            key = identity_key(clean)
            if clean and key and key not in seen:
                result.append(clean[:120])
                seen.add(key)
        return result[:8]
