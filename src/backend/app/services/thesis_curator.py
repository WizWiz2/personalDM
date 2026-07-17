import json
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from uuid import UUID

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.provider_config_repo import ProviderConfigRepository
from app.db.repositories.scene_repo import SceneRepository
from app.models.scene_thesis import SceneThesisCreate, SceneThesisUpdate, ThesisType
from app.models.turn import ChatMessage
from app.providers.llm_provider import LLMProvider, LLMProviderError


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
    """Maintain a coherent, current working-memory snapshot for one scene."""

    PARAPHRASE_SIMILARITY = 0.86

    def __init__(self, session: AsyncSession):
        self._session = session
        self._scene_repo = SceneRepository(session)
        self._entity_repo = EntityRepository(session)
        self._config_repo = ProviderConfigRepository(session)
        self._llm_provider = LLMProvider()

    @staticmethod
    def scope_key(thesis_type: str, related_entity_ids: list[UUID]) -> str:
        entity_scope = ",".join(sorted(str(value) for value in related_entity_ids))
        return f"{thesis_type}:{entity_scope or 'scene'}"

    @staticmethod
    def _normalized_text(value: str) -> str:
        value = value.casefold().replace("ё", "е")
        return " ".join(re.findall(r"[\w]+", value, flags=re.UNICODE))

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
        return len(active)

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

        config = await self._config_repo.get_by_campaign_id(campaign_id)
        if not config:
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
            current_lines.append(
                json.dumps(
                    {
                        "id": str(thesis.id),
                        "scope": self.scope_key(
                            thesis.thesis_type,
                            thesis.related_entity_ids,
                        ),
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

        entity_lines = [
            f"- {name}: {entity_id}" for entity_id, name in entity_names.items()
        ]
        prompt = f"""Ты куратор живых тезисов сцены настольной RPG.
Верни ПОЛНЫЙ желаемый набор актуальных незакреплённых тезисов после хода.
Все тексты тезисов пиши только на русском языке.

Тезис — краткоживущая рабочая память ДМа. Это не биография, не инвентарь,
не вечный канон и не пересказ старой истории.

ПРАВИЛА:
- Обычно держи 4-10 тезисов, но не заполняй квоту искусственно.
- Набор должен быть внутренне непротиворечивым.
- Один смысловой слот: thesis_type + одинаковые related_entity_ids.
- При сохранении смысла укажи existing_thesis_id и не перефразируй ради стиля.
- Замени тезис только когда изменилось состояние мира, намерение или напряжение.
- Оставь завершённые и устаревшие тезисы вне ответа.
- Не повторяй pinned тезисы и не противоречь им.
- Попытка игрока не становится правдой без подтверждения ДМа.
- Используй только перечисленные UUID персонажей.
- Не записывай запланированное событие как уже случившееся.

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

Завершённый ход:
ПОПЫТКА ИГРОКА: {user_content}
РЕЗУЛЬТАТ ДМА: {assistant_content}

Верни только JSON:
{{"desired_active":[{{"thesis_type":"tension","text":"...","priority":5,"visibility":"dm","related_entity_ids":[],"existing_thesis_id":null,"semantic_key":"короткий стабильный ключ"}}]}}
"""

        api_key = await self._config_repo.get_decrypted_key(campaign_id)
        response_text = ""
        try:
            async for token in self._llm_provider.generate_stream(
                [ChatMessage(role="system", content=prompt)],
                config,
                api_key,
                max_tokens=900,
                temperature=0.1,
            ):
                response_text += token
        except LLMProviderError:
            return None

        desired = self._parse_response(response_text, set(entity_names))
        if desired is None:
            return None
        return await self.reconcile(scene_id, source_turn_id, desired)

    @staticmethod
    def _parse_response(
        text: str,
        allowed_entity_ids: set[str],
    ) -> list[DesiredThesis] | None:
        clean = text.strip()
        if clean.startswith("```"):
            lines = clean.splitlines()
            clean = "\n".join(lines[1:-1]).strip()
        try:
            parsed = CuratorResponse.model_validate(json.loads(clean))
        except (json.JSONDecodeError, ValidationError, TypeError):
            return None

        result = []
        for thesis in parsed.desired_active:
            if thesis.visibility not in {"dm", "public", "character_only"}:
                continue
            if any(
                str(entity_id) not in allowed_entity_ids
                for entity_id in thesis.related_entity_ids
            ):
                continue
            result.append(thesis)
        return result

    async def _group_active(self, scene_id: UUID):
        active = await self._scene_repo.list_theses_by_scene(scene_id, active_only=True)
        groups: dict[str, list] = {}
        for thesis in active:
            groups.setdefault(
                self.scope_key(thesis.thesis_type, thesis.related_entity_ids),
                [],
            ).append(thesis)
        return groups

    async def reconcile(
        self,
        scene_id: UUID,
        source_turn_id: UUID,
        desired: list[DesiredThesis],
    ) -> ThesisReconcileResult:
        result = ThesisReconcileResult()
        groups = await self._group_active(scene_id)

        pinned_by_scope = {}
        mutable_by_scope = {}
        for scope, items in groups.items():
            pinned = [item for item in items if item.pinned]
            mutable = [item for item in items if not item.pinned]

            if pinned:
                keeper = self._choose_current(pinned)
                pinned_by_scope[scope] = keeper
                for duplicate in pinned:
                    if duplicate.id == keeper.id:
                        continue
                    await self._scene_repo.update_thesis(
                        duplicate.id,
                        SceneThesisUpdate(status="resolved"),
                    )
                    result.duplicate_scopes += 1
                    result.resolved += 1

            if mutable:
                keeper = self._choose_current(mutable)
                mutable_by_scope[scope] = keeper
                for duplicate in mutable:
                    if duplicate.id == keeper.id:
                        continue
                    await self._scene_repo.update_thesis(
                        duplicate.id,
                        SceneThesisUpdate(status="superseded"),
                    )
                    result.duplicate_scopes += 1
                    result.superseded += 1

        desired_by_scope: dict[str, DesiredThesis] = {}
        for item in sorted(desired, key=lambda value: value.priority, reverse=True):
            scope = self.scope_key(item.thesis_type.value, item.related_entity_ids)
            if scope in desired_by_scope:
                result.duplicate_scopes += 1
                continue
            if scope in pinned_by_scope:
                pinned = pinned_by_scope[scope]
                if self._similarity(pinned.text, item.text) < self.PARAPHRASE_SIMILARITY:
                    result.pinned_conflicts += 1
                else:
                    result.kept += 1
                continue
            desired_by_scope[scope] = item

        for scope, old in mutable_by_scope.items():
            new = desired_by_scope.get(scope)
            if new is None:
                await self._scene_repo.update_thesis(
                    old.id,
                    SceneThesisUpdate(status="resolved"),
                )
                result.resolved += 1
                continue

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
                    await self._scene_repo.update_thesis(
                        old.id,
                        SceneThesisUpdate(**changes),
                    )
                result.kept += 1
                if old.text.strip() != new.text.strip():
                    result.paraphrases_ignored += 1
                desired_by_scope.pop(scope)
                continue

            await self._scene_repo.update_thesis(
                old.id,
                SceneThesisUpdate(status="superseded"),
            )
            await self._scene_repo.create_thesis(
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
            desired_by_scope.pop(scope)
            result.superseded += 1

        for new in desired_by_scope.values():
            await self._scene_repo.create_thesis(
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
            result.created += 1

        await self._session.flush()
        return result
