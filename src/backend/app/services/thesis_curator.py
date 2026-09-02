import json
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from uuid import UUID

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.event_repo import EventRepository
from app.db.repositories.fact_repo import FactRepository
from app.db.repositories.provider_config_repo import ProviderConfigRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.repositories.turn_repo import TurnRepository
from app.models.scene_thesis import SceneThesisCreate, SceneThesisUpdate, ThesisType
from app.models.turn import ChatMessage
from app.providers.llm_provider import LLMProvider, LLMProviderError
from app.services.memory_operations import MemoryOperationsService
from app.services.role_model_router import ModelRole, RoleModelRouter


class DesiredThesis(BaseModel):
    thesis_type: ThesisType
    text: str = Field(min_length=3, max_length=800)
    priority: int = Field(default=0, ge=-10, le=10)
    visibility: str = "dm"
    related_entity_ids: list[UUID] = Field(default_factory=list)
    existing_thesis_id: UUID | None = None
    semantic_key: str | None = Field(default=None, max_length=160)


class CuratorResponse(BaseModel):
    desired_active: list[DesiredThesis] = Field(default_factory=list, max_length=12)
    resolve_thesis_ids: list[UUID] = Field(default_factory=list, max_length=12)


@dataclass
class ThesisReconcileResult:
    created: int = 0
    superseded: int = 0
    resolved: int = 0
    kept: int = 0
    pinned_conflicts: int = 0
    duplicate_scopes: int = 0
    paraphrases_ignored: int = 0


class ThesisCurator:
    """Maintain coherent scene working memory without treating omission as resolution."""

    PARAPHRASE_SIMILARITY = 0.86

    def __init__(self, session: AsyncSession):
        self._session = session
        self._scene_repo = SceneRepository(session)
        self._entity_repo = EntityRepository(session)
        self._fact_repo = FactRepository(session)
        self._event_repo = EventRepository(session)
        self._turn_repo = TurnRepository(session)
        self._config_repo = ProviderConfigRepository(session)
        self._model_router = RoleModelRouter(self._config_repo)
        self._llm_provider = LLMProvider()

    @staticmethod
    def _supports_lifecycle(session) -> bool:
        return callable(getattr(session, "execute", None)) and callable(
            getattr(session, "flush", None)
        )

    @staticmethod
    def scope_key(thesis_type: str, related_entity_ids: list[UUID]) -> str:
        entity_scope = ",".join(sorted(str(value) for value in related_entity_ids))
        return f"{thesis_type}:{entity_scope or 'scene'}"

    @staticmethod
    def _normalized_text(value: str) -> str:
        value = value.casefold().replace("ё", "е")
        return " ".join(re.findall(r"[\w]+", value, flags=re.UNICODE))

    @classmethod
    def _semantic_key(cls, value: str | None, fallback_text: str) -> str:
        source = value or fallback_text
        normalized = cls._normalized_text(source)
        return normalized[:160] or "thesis"

    @classmethod
    def semantic_slot(
        cls,
        thesis_type: str,
        related_entity_ids: list[UUID],
        semantic_key: str,
    ) -> str:
        return f"{cls.scope_key(thesis_type, related_entity_ids)}:{semantic_key}"

    @classmethod
    def _similarity(cls, left: str, right: str) -> float:
        return SequenceMatcher(
            None,
            cls._normalized_text(left),
            cls._normalized_text(right),
        ).ratio()

    @staticmethod
    def _choose_current(items):
        return max(
            items,
            key=lambda item: (
                int(item.pinned),
                item.priority,
                item.updated_at,
                item.created_at,
            ),
        )

    async def _existing_semantic_key(self, thesis) -> str:
        fallback = self._semantic_key(None, thesis.text)
        if not self._supports_lifecycle(self._session):
            return fallback
        profile = await MemoryOperationsService(self._session).ensure_thesis_profile(
            thesis,
            semantic_key=None,
        )
        return self._semantic_key(profile.semantic_key, thesis.text)

    async def _reinforce_profile(
        self,
        thesis,
        source_turn_id: UUID,
        semantic_key: str,
    ) -> None:
        if not self._supports_lifecycle(self._session):
            return
        await MemoryOperationsService(self._session).ensure_thesis_profile(
            thesis,
            reinforced_turn_id=source_turn_id,
            semantic_key=semantic_key,
        )

    async def _mark_profile_closed(self, thesis, reason: str) -> None:
        if not self._supports_lifecycle(self._session):
            return
        profile = await MemoryOperationsService(self._session).ensure_thesis_profile(thesis)
        profile.closure_reason = reason
        await self._session.flush()

    async def close_scene(self, scene_id: UUID) -> int:
        """Resolve every operational thesis when a scene ends.

        Durable consequences must already have been written as facts, events,
        relationships or beliefs. A thesis is working memory and must not remain active
        after its scene is completed, including pinned director instructions.
        """
        active = await self._scene_repo.list_theses_by_scene(scene_id, active_only=True)
        for thesis in active:
            await self._scene_repo.update_thesis(
                thesis.id,
                SceneThesisUpdate(status="resolved"),
            )
        await self._session.flush()
        if self._supports_lifecycle(self._session):
            await MemoryOperationsService(self._session).record_closed_scene(scene_id)
        return len(active)

    async def _recent_scene_window(
        self,
        campaign_id: UUID,
        scene_id: UUID,
        user_content: str,
        assistant_content: str,
        entity_names: dict[str, str],
    ) -> list[str]:
        pair_count = max(2, int(settings.CURATOR_INTERVAL_TURNS))
        history = await self._turn_repo.get_history(
            campaign_id,
            limit=max(12, pair_count * 4),
            active_only=True,
            channel="narrative",
        )
        relevant = [
            turn
            for turn in history
            if turn.scene_id == scene_id and turn.role in {"user", "assistant"}
        ][-(pair_count * 2) :]
        lines: list[str] = []
        for turn in relevant:
            if turn.role == "user":
                label = "ИГРОК"
            elif turn.acting_character_id:
                label = entity_names.get(str(turn.acting_character_id), "NPC")
            else:
                label = "ДМ"
            lines.append(f"{label}: {turn.content}")

        current_result = f"ДМ: {assistant_content}"
        if not lines or lines[-1] != current_result:
            lines.extend([f"ИГРОК: {user_content}", current_result])
        return lines[-(pair_count * 2) :]

    async def curate_after_turn(
        self,
        campaign_id: UUID,
        scene_id: UUID | None,
        source_turn_id: UUID,
        user_content: str,
        assistant_content: str,
    ) -> ThesisReconcileResult | None:
        if not scene_id or not assistant_content.strip():
            return None

        selection = await self._model_router.resolve(campaign_id, ModelRole.CURATOR)
        if selection is None:
            return None

        scene = await self._scene_repo.get_by_id(scene_id)
        if not scene or scene.status != "active":
            return None

        active = await self._scene_repo.list_theses_by_scene(scene_id, active_only=True)
        entity_names: dict[str, str] = {}
        for entity_id in scene.participants:
            entity = await self._entity_repo.get_by_id(entity_id)
            if entity:
                entity_names[str(entity.id)] = entity.canonical_name

        current_lines = []
        for thesis in active:
            names = [
                entity_names.get(str(value), str(value))
                for value in thesis.related_entity_ids
            ]
            semantic_key = await self._existing_semantic_key(thesis)
            current_lines.append(
                json.dumps(
                    {
                        "id": str(thesis.id),
                        "scope": self.scope_key(
                            thesis.thesis_type,
                            thesis.related_entity_ids,
                        ),
                        "semantic_key": semantic_key,
                        "type": thesis.thesis_type,
                        "text": thesis.text,
                        "priority": thesis.priority,
                        "visibility": thesis.visibility,
                        "related_entities": names,
                        "related_entity_ids": [
                            str(value) for value in thesis.related_entity_ids
                        ],
                        "pinned": thesis.pinned,
                    },
                    ensure_ascii=False,
                )
            )

        facts = await self._fact_repo.list_active(campaign_id, scene_id=scene_id)
        fact_lines = [
            f"- {fact.subject} | {fact.predicate} | {fact.object_value or 'null'} "
            f"[{fact.truth_status}; {fact.scope}]"
            for fact in facts[-24:]
        ]
        events = await self._event_repo.list_by_campaign(campaign_id)
        event_lines = [
            f"- {event.event_type}: {event.description}"
            for event in events[:12]
        ]
        recent_lines = await self._recent_scene_window(
            campaign_id,
            scene_id,
            user_content,
            assistant_content,
            entity_names,
        )

        entity_lines = [
            f"- {name}: {entity_id}" for entity_id, name in entity_names.items()
        ]
        prompt = f"""Ты куратор живых тезисов сцены настольной RPG.
Верни только изменения рабочей памяти, которые действительно подтверждены данным окном ходов.
Все тексты тезисов пиши только на русском языке.

Тезис — краткоживущая рабочая память ДМа. Это не биография, не инвентарь,
не вечный канон и не пересказ старой истории.

КРИТИЧЕСКАЯ СЕМАНТИКА:
- semantic_key определяет отдельную смысловую нить внутри thesis_type + related_entity_ids.
- Разные semantic_key могут и должны жить одновременно даже при одинаковом thesis_type и scope.
- Для существующего смысла сохраняй прежний semantic_key и existing_thesis_id.
- desired_active содержит только новые, изменившиеся или явно подтверждённые этим окном тезисы.
- ПРОПУСК существующего тезиса НЕ означает, что он завершён: он останется жить до TTL.
- resolve_thesis_ids содержит только ID тезисов, которые окно ходов ЯВНО завершило или опровергло.
- Никогда не resolve pinned тезис.
- Не закрывай сюжетную нить только потому, что последние реплики были о другом.
- Попытка игрока не становится правдой без подтверждения ДМа.
- Structured facts/events ниже приоритетнее пересказа модели и являются источником истины.
- Используй только перечисленные UUID персонажей.
- Не записывай запланированное событие как уже случившееся.
- Обычно одновременно может жить 4-10 тезисов, но не заполняй квоту искусственно.

Допустимые thesis_type:
canon, intention, relationship_dynamic, secret, tension, unresolved_beat,
visual_state, music_mood

Сцена: {scene.title}
{scene.location_description or ''}
Настроение: {scene.mood or ''}; напряжение: {scene.tension or ''}

Участники:
{chr(10).join(entity_lines) or '- нет'}

Текущие тезисы:
{chr(10).join(current_lines) or '- нет'}

Актуальные structured facts:
{chr(10).join(fact_lines) or '- нет'}

Недавние structured events:
{chr(10).join(event_lines) or '- нет'}

Окно ходов с прошлого запуска Curator:
{chr(10).join(recent_lines) or '- нет'}

Верни только JSON:
{{
  "desired_active":[
    {{"thesis_type":"tension","text":"...","priority":5,"visibility":"dm","related_entity_ids":[],"existing_thesis_id":null,"semantic_key":"короткий стабильный ключ"}}
  ],
  "resolve_thesis_ids":[]
}}
"""

        response_data = await self._model_router.generate_json(
            self._llm_provider,
            selection,
            [ChatMessage(role="system", content=prompt)],
            max_tokens=1100,
            temperature=0.1,
            response_model=CuratorResponse,
        )

        envelope = self._validate_envelope(
            response_data,
            set(entity_names),
            {str(thesis.id) for thesis in active},
        )
        if envelope is None:
            raise LLMProviderError("Thesis Curator returned invalid structured output")
        return await self.reconcile(
            scene_id,
            source_turn_id,
            envelope.desired_active,
            resolve_thesis_ids=set(envelope.resolve_thesis_ids),
        )

    @staticmethod
    def _validate_envelope(
        data: dict,
        allowed_entity_ids: set[str],
        active_thesis_ids: set[str] | None = None,
    ) -> CuratorResponse | None:
        try:
            parsed = CuratorResponse.model_validate(data)
        except (ValidationError, TypeError):
            return None

        desired = []
        for thesis in parsed.desired_active:
            if thesis.visibility not in {"dm", "public", "character_only"}:
                continue
            if any(
                str(entity_id) not in allowed_entity_ids
                for entity_id in thesis.related_entity_ids
            ):
                continue
            desired.append(thesis)

        allowed_theses = active_thesis_ids or set()
        resolve_ids = [
            thesis_id
            for thesis_id in parsed.resolve_thesis_ids
            if not allowed_theses or str(thesis_id) in allowed_theses
        ]
        return CuratorResponse(
            desired_active=desired,
            resolve_thesis_ids=resolve_ids,
        )

    @classmethod
    def _validate_response(
        cls,
        data: dict,
        allowed_entity_ids: set[str],
    ) -> list[DesiredThesis] | None:
        envelope = cls._validate_envelope(data, allowed_entity_ids)
        return envelope.desired_active if envelope is not None else None

    @classmethod
    def _parse_response(
        cls,
        text: str,
        allowed_entity_ids: set[str],
    ) -> list[DesiredThesis] | None:
        clean = text.strip()
        if clean.startswith("```"):
            lines = clean.splitlines()
            clean = "\n".join(lines[1:-1]).strip()
        try:
            data = json.loads(clean)
        except (json.JSONDecodeError, TypeError):
            return None
        return cls._validate_response(data, allowed_entity_ids)

    async def _group_active(self, scene_id: UUID):
        active = await self._scene_repo.list_theses_by_scene(scene_id, active_only=True)
        groups: dict[str, list] = {}
        semantic_keys: dict[UUID, str] = {}
        slots_by_id: dict[UUID, str] = {}
        for thesis in active:
            semantic_key = await self._existing_semantic_key(thesis)
            slot = self.semantic_slot(
                thesis.thesis_type,
                thesis.related_entity_ids,
                semantic_key,
            )
            groups.setdefault(slot, []).append(thesis)
            semantic_keys[thesis.id] = semantic_key
            slots_by_id[thesis.id] = slot
        return groups, semantic_keys, slots_by_id

    async def reconcile(
        self,
        scene_id: UUID,
        source_turn_id: UUID,
        desired: list[DesiredThesis],
        *,
        resolve_thesis_ids: set[UUID] | None = None,
    ) -> ThesisReconcileResult:
        result = ThesisReconcileResult()
        groups, semantic_keys, slots_by_id = await self._group_active(scene_id)
        resolve_ids = set(resolve_thesis_ids or set())

        pinned_by_slot = {}
        mutable_by_slot = {}
        active_by_id = {}
        for slot, items in groups.items():
            for item in items:
                active_by_id[item.id] = item
            pinned = [item for item in items if item.pinned]
            mutable = [item for item in items if not item.pinned]

            if pinned:
                keeper = self._choose_current(pinned)
                pinned_by_slot[slot] = keeper
                for duplicate in pinned:
                    if duplicate.id == keeper.id:
                        continue
                    await self._scene_repo.update_thesis(
                        duplicate.id,
                        SceneThesisUpdate(status="resolved"),
                    )
                    await self._mark_profile_closed(duplicate, "duplicate_semantic_slot")
                    result.duplicate_scopes += 1
                    result.resolved += 1

            if mutable:
                keeper = self._choose_current(mutable)
                mutable_by_slot[slot] = keeper
                for duplicate in mutable:
                    if duplicate.id == keeper.id:
                        continue
                    await self._scene_repo.update_thesis(
                        duplicate.id,
                        SceneThesisUpdate(status="superseded"),
                    )
                    await self._mark_profile_closed(duplicate, "duplicate_semantic_slot")
                    result.duplicate_scopes += 1
                    result.superseded += 1

        desired_by_slot: dict[str, tuple[DesiredThesis, str]] = {}
        for item in sorted(desired, key=lambda value: value.priority, reverse=True):
            if item.existing_thesis_id in active_by_id:
                slot = slots_by_id[item.existing_thesis_id]
                semantic_key = semantic_keys[item.existing_thesis_id]
            else:
                semantic_key = self._semantic_key(item.semantic_key, item.text)
                slot = self.semantic_slot(
                    item.thesis_type.value,
                    item.related_entity_ids,
                    semantic_key,
                )
            if slot in desired_by_slot:
                result.duplicate_scopes += 1
                continue
            if slot in pinned_by_slot:
                pinned = pinned_by_slot[slot]
                if self._similarity(pinned.text, item.text) < self.PARAPHRASE_SIMILARITY:
                    result.pinned_conflicts += 1
                else:
                    result.kept += 1
                continue
            desired_by_slot[slot] = (item, semantic_key)

        for slot, old in mutable_by_slot.items():
            if old.id in resolve_ids:
                await self._scene_repo.update_thesis(
                    old.id,
                    SceneThesisUpdate(status="resolved"),
                )
                await self._mark_profile_closed(old, "curator_explicit_resolution")
                result.resolved += 1
                desired_by_slot.pop(slot, None)
                continue

            pair = desired_by_slot.get(slot)
            if pair is None:
                # Omission is not evidence of completion. The lifecycle profile/TTL owns
                # eventual expiry, so unrelated turns cannot silently erase open threads.
                result.kept += 1
                continue

            new, semantic_key = pair
            same_identity = new.existing_thesis_id in {None, old.id}
            similarity = self._similarity(old.text, new.text)
            semantically_same = same_identity and similarity >= self.PARAPHRASE_SIMILARITY
            if semantically_same:
                changes = {}
                if old.priority != new.priority:
                    changes["priority"] = new.priority
                if old.visibility != new.visibility:
                    changes["visibility"] = new.visibility
                if changes:
                    updated = await self._scene_repo.update_thesis(
                        old.id,
                        SceneThesisUpdate(**changes),
                    )
                    if updated is not None:
                        old = updated
                await self._reinforce_profile(old, source_turn_id, semantic_key)
                result.kept += 1
                if old.text.strip() != new.text.strip():
                    result.paraphrases_ignored += 1
                desired_by_slot.pop(slot)
                continue

            await self._scene_repo.update_thesis(
                old.id,
                SceneThesisUpdate(status="superseded"),
            )
            await self._mark_profile_closed(old, "curator_update")
            created = await self._scene_repo.create_thesis(
                scene_id,
                SceneThesisCreate(
                    thesis_type=new.thesis_type,
                    text=new.text,
                    priority=new.priority,
                    visibility=new.visibility,
                    related_entity_ids=new.related_entity_ids,
                ),
                source_turn_id=source_turn_id,
            )
            await self._reinforce_profile(created, source_turn_id, semantic_key)
            desired_by_slot.pop(slot)
            result.superseded += 1

        for new, semantic_key in desired_by_slot.values():
            created = await self._scene_repo.create_thesis(
                scene_id,
                SceneThesisCreate(
                    thesis_type=new.thesis_type,
                    text=new.text,
                    priority=new.priority,
                    visibility=new.visibility,
                    related_entity_ids=new.related_entity_ids,
                ),
                source_turn_id=source_turn_id,
            )
            await self._reinforce_profile(created, source_turn_id, semantic_key)
            result.created += 1

        await self._session.flush()
        return result