from __future__ import annotations

import asyncio
import json
import re
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.goal_repo import GoalRepository
from app.db.repositories.location_repo import LocationRepository
from app.db.repositories.provider_config_repo import ProviderConfigRepository
from app.models.character import CharacterCreate
from app.models.goal import GoalCreate
from app.models.location import LocationCreate
from app.models.session_zero import SessionZeroUpdate
from app.models.session_zero_interview import (
    SessionZeroAgentToolCall,
    SessionZeroInterviewDecision,
    SessionZeroInterviewDraft,
    SessionZeroInterviewModelDecision,
    SessionZeroInterviewPatch,
    SessionZeroInterviewState,
)
from app.models.turn import ChatMessage
from app.providers.llm_provider import LLMProvider, LLMProviderError
from app.services.role_model_router import ModelRole, RoleModelRouter
from app.services.session_zero_service import SessionZeroService


class SessionZeroInterviewIncompleteError(ValueError):
    def __init__(self, missing_fields: list[str]):
        self.missing_fields = missing_fields
        super().__init__(
            "Session-zero interview is incomplete: " + ", ".join(missing_fields)
        )


class SessionZeroAgent:
    """A conversational agent that owns Session Zero dialogue decisions.

    The application supplies conversation history and the accumulated draft. The agent
    decides what to say and emits provider-neutral tool calls. It is deliberately not
    given a scripted question order.
    """

    MAX_HISTORY_MESSAGES = 12
    MODEL_RESPONSE_TOKENS = 1200
    RATE_LIMIT_RETRY_CAP_SECONDS = 12.0
    SYSTEM_PROMPT = """[PERSONAL DM — SESSION ZERO AGENT]
Ты отдельный нейро-мастер, который проводит живую нулевую сессию для одного игрока.
Это разговор, а не анкета и не последовательное заполнение полей.

ГЛАВНАЯ ЗАДАЧА
- Понять, во что человеку хочется играть, помочь сформировать героя и естественную
  стартовую ситуацию.
- Вести беседу самостоятельно: реагировать на смысл ответа, предлагать идеи, объединять
  связанные вопросы и менять тему, когда игроку нечего добавить.
- Не перечислять поля карточки и не допрашивать игрока о каждом пункте.

ИЗВЕСТНЫЕ СЕТТИНГИ
- Если игрок называет известный сеттинг, систему или вселенную, используй свои знания о
  ней. Не проси игрока пересказывать базовый канон и не делай вид, что название тебе
  неизвестно.
- По умолчанию считай, что берётся узнаваемый канон. Уточняй только важные отклонения,
  эпоху, редакцию или акцент, если это действительно влияет на игру.
- Например, после «В Shadowrun» уместно коротко подтвердить понимание мира и перейти к
  желаемому акценту или герою, одновременно сохранив базовые сведения о сеттинге.

ЕСТЕСТВЕННЫЙ РАЗГОВОР
- Отвечай только по-русски, кроме собственных имён и официальных названий.
- Обычно задавай не больше одного вопроса; два тесно связанных вопроса допустимы, если
  в разговоре это звучит естественно.
- Если игрок говорит «не знаю», «не могу сказать», «без разницы» или передаёт выбор
  мастеру, выбери разумный безопасный вариант для мира, тона, стиля или старта и двигайся
  дальше. Не задавай тот же вопрос повторно.
- Не повторяй уже заданный вопрос и не отвечай пустыми фразами вроде «Продолжим нулевую
  сессию» без содержательного движения разговора.
- Не придумывай за игрока его текущие решения, реплики и чувства.
- Личные границы считаются подтверждёнными только после явного ответа игрока. Их нельзя
  выводить из сеттинга или характера героя.

КАРТОЧКА
- Сохраняй подтверждённые факты и разумные низкорисковые выводы через инструмент
  update_session_zero.
- Подробная карточка нужна, но её не обязательно собирать отдельным вопросом на каждое
  поле. Часть внешности, манеры речи, биографии, способностей и ограничений можно вывести
  из цельного концепта или предложить самому.
- Ценности, страхи и желания можно аккуратно предложить как интерпретацию, но итоговая
  сводка будет показана игроку для подтверждения.
- Не стирай и не переписывай подтверждённые данные без явной поправки игрока.

СТАРТОВОЕ ПРИСУТСТВИЕ NPC
- Когда стартовая ситуация становится понятной, ОБЯЗАТЕЛЬНО запиши в
  world.starter_npcs полный список NPC, которые физически присутствуют в первой сцене,
  и одновременно поставь world.starter_presence_confirmed=true.
- Это не список всех персонажей завязки. Не включай пропавших, тех, кого ещё надо найти,
  далёких владельцев, будущих собеседников, фоновые имена или людей, которые только
  упомянуты в истории.
- Если в первой сцене никого кроме героя нет, запиши starter_npcs=[] и всё равно поставь
  starter_presence_confirmed=true.
- Если в своей обычной реплике ты показываешь, что «владелица входит», «компаньон сидит
  напротив», «заказчик ждёт у двери» и т.п., этот же NPC обязан быть в starter_npcs.
- role описывает его функцию в сцене; name указывай только если имя уже установлено.
  Не придумывай имя только ради структуры. description/reason могут быть короткими.
- Не используй профессию или отдельное слово как доказательство физического присутствия:
  решай присутствие по смыслу всей стартовой ситуации.

ИНСТРУМЕНТЫ
1. update_session_zero — скрыто обновляет только новые данные текущего хода.
   Передай patch с секциями world и character. Не копируй карточку целиком.
2. finalize_session_zero — сообщи, что разговор естественно готов перейти к итоговой
   сводке. Вызывай только когда карточка достаточно цельная и старт можно запустить.

Верни один короткий JSON:
{
  "assistant_message": "обычная живая реплика мастера",
  "tool_calls": [
    {"name": "update_session_zero", "patch": {"world": {}, "character": {}}}
  ],
  "question_topics": []
}

question_topics — необязательная диагностическая метка. Код не выбирает по ней вопрос и
не заменяет твою реплику. Не показывай названия инструментов или JSON игроку.
"""

    def __init__(
        self,
        provider: LLMProvider,
        router: RoleModelRouter,
    ) -> None:
        self._provider = provider
        self._router = router

    async def respond(
        self,
        selection,
        state: SessionZeroInterviewState,
        *,
        feedback: dict | None = None,
    ) -> SessionZeroInterviewModelDecision:
        current = json.dumps(
            state.draft.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        gaps = SessionZeroInterviewService.missing_fields(state.draft)
        system_content = (
            f"{self.SYSTEM_PROMPT}\n\n"
            f"[ТЕКУЩАЯ КАРТОЧКА — ТОЛЬКО ДЛЯ ЧТЕНИЯ]\n{current}\n\n"
            "[ПРОБЕЛЫ КАРТОЧКИ — ЭТО НЕ СПИСОК ВОПРОСОВ ИГРОКУ]\n"
            f"{json.dumps(gaps, ensure_ascii=False)}"
        )
        messages = [
            ChatMessage(role="system", content=system_content),
            *[
                ChatMessage(role=item["role"], content=item["content"])
                for item in state.messages[-self.MAX_HISTORY_MESSAGES :]
                if item.get("role") in {"user", "assistant"}
                and item.get("content")
            ],
        ]
        if feedback:
            messages.append(
                ChatMessage(
                    role="system",
                    content=(
                        "[РЕЗУЛЬТАТ ИНСТРУМЕНТА / FEEDBACK]\n"
                        + json.dumps(feedback, ensure_ascii=False)
                        + "\nСам реши, как естественно продолжить разговор. Не перечисляй "
                        "поля и не превращай ответ в анкету."
                    ),
                )
            )
        data = await self._generate(selection, messages)
        return SessionZeroInterviewModelDecision.model_validate(data)

    async def _generate(self, selection, messages: list[ChatMessage]) -> dict:
        try:
            return await self._router.generate_json(
                self._provider,
                selection,
                messages,
                max_tokens=self.MODEL_RESPONSE_TOKENS,
                temperature=0.35,
                response_model=SessionZeroInterviewModelDecision,
            )
        except LLMProviderError as exc:
            delay = self._rate_limit_retry_seconds(str(exc))
            if delay is None:
                raise
            await asyncio.sleep(delay)
            return await self._router.generate_json(
                self._provider,
                selection,
                messages,
                max_tokens=self.MODEL_RESPONSE_TOKENS,
                temperature=0.0,
                response_model=SessionZeroInterviewModelDecision,
            )

    @classmethod
    def _rate_limit_retry_seconds(cls, error: str) -> float | None:
        folded = error.casefold()
        if "http 429" not in folded and "rate_limit" not in folded:
            return None
        match = re.search(
            r"(?:try again in|retry after)\s*([0-9]+(?:\.[0-9]+)?)\s*s",
            error,
            re.IGNORECASE,
        )
        if not match:
            return 1.0
        return min(
            cls.RATE_LIMIT_RETRY_CAP_SECONDS,
            max(0.25, float(match.group(1))),
        )


class SessionZeroInterviewService:
    """Persistence and tool-execution boundary for the conversational agent."""

    STATE_KEY = "session_zero_interview"
    MAX_HISTORY_MESSAGES = SessionZeroAgent.MAX_HISTORY_MESSAGES
    MODEL_RESPONSE_TOKENS = SessionZeroAgent.MODEL_RESPONSE_TOKENS
    RATE_LIMIT_RETRY_CAP_SECONDS = SessionZeroAgent.RATE_LIMIT_RETRY_CAP_SECONDS
    OPENING_MESSAGE = (
        "Во что тебе хочется сыграть именно сейчас? Можно начать с мира, жанра, "
        "героя или просто с ощущения, которое хочется получить от кампании."
    )
    CORRECTION_MARKERS = (
        "исправ",
        "не так",
        "замени",
        "передумал",
        "на самом деле",
        "точнее",
        "поправка",
        "пусть будет",
    )

    def __init__(self, session: AsyncSession):
        self._session = session
        self._session_zero = SessionZeroService(session)
        self._entities = EntityRepository(session)
        self._locations = LocationRepository(session)
        self._goals = GoalRepository(session)
        self._provider = LLMProvider()
        self._router = RoleModelRouter(ProviderConfigRepository(session))
        self._agent = SessionZeroAgent(self._provider, self._router)

    async def get_state(self, campaign_id: UUID) -> SessionZeroInterviewState:
        setup = await self._session_zero.get(campaign_id)
        raw = setup.custom_fields.get(self.STATE_KEY)
        if not isinstance(raw, dict):
            return SessionZeroInterviewState()
        try:
            return SessionZeroInterviewState.model_validate(raw)
        except ValueError:
            return SessionZeroInterviewState()

    async def answer(
        self,
        campaign_id: UUID,
        user_message: str,
    ) -> SessionZeroInterviewDecision:
        clean = " ".join(user_message.split())
        if not clean:
            raise ValueError("Session-zero answer cannot be empty")
        state = await self.get_state(campaign_id)
        if state.pending_user_message:
            raise ValueError(
                "A previous answer is waiting for the model; retry it before adding another"
            )

        state.messages.append({"role": "user", "content": clean})
        state.pending_user_message = clean
        await self._save_state(campaign_id, state, commit=True)
        return await self._continue_pending(campaign_id, state)

    async def retry_pending(
        self,
        campaign_id: UUID,
    ) -> SessionZeroInterviewDecision | None:
        state = await self.get_state(campaign_id)
        if not state.pending_user_message:
            return None
        return await self._continue_pending(campaign_id, state)

    async def _continue_pending(
        self,
        campaign_id: UUID,
        state: SessionZeroInterviewState,
    ) -> SessionZeroInterviewDecision:
        selection = await self._router.resolve(campaign_id, ModelRole.SESSION_ZERO)
        if selection is None:
            raise LLMProviderError("No LLM provider is configured for this campaign")

        latest_user_message = state.pending_user_message or ""
        model_decision = await self._agent.respond(selection, state)
        merged, finalize_requested = self._execute_tool_calls(
            state.draft,
            model_decision.tool_calls,
            explicit_correction=self._is_explicit_correction(latest_user_message),
        )

        feedback = self._quality_feedback(model_decision, state, merged)
        if finalize_requested:
            missing = self.missing_fields(merged)
            if missing:
                feedback = {
                    "tool": "finalize_session_zero",
                    "ok": False,
                    "missing_fields": missing,
                    "instruction": (
                        "Карточка ещё не готова. Заполни безопасные выводимые детали "
                        "сам через update_session_zero, а если без игрока нельзя — задай "
                        "один естественный содержательный вопрос."
                    ),
                }

        if feedback:
            state.draft = merged
            repaired = await self._agent.respond(
                selection,
                state,
                feedback=feedback,
            )
            merged, repaired_finalize = self._execute_tool_calls(
                merged,
                repaired.tool_calls,
                explicit_correction=self._is_explicit_correction(latest_user_message),
            )
            finalize_requested = finalize_requested or repaired_finalize
            model_decision = repaired

        missing = self.missing_fields(merged)
        ready = finalize_requested and not missing
        decision = SessionZeroInterviewDecision(
            assistant_message=model_decision.assistant_message.strip(),
            ready_to_finalize=ready,
            draft=merged,
            missing_topics=missing,
            question_topics=model_decision.question_topics,
            summary=self.summary(merged),
        )
        state.draft = merged
        state.messages.append(
            {"role": "assistant", "content": decision.assistant_message}
        )
        state.pending_user_message = None
        state.last_question_topics = decision.question_topics
        state.last_summary = decision.summary
        await self._save_state(campaign_id, state, commit=True)
        return decision

    def _quality_feedback(
        self,
        decision: SessionZeroInterviewModelDecision,
        state: SessionZeroInterviewState,
        draft: SessionZeroInterviewDraft,
    ) -> dict | None:
        message = " ".join(decision.assistant_message.split())
        normalized = self._normalize_text(message)
        previous = {
            self._normalize_text(item.get("content", ""))
            for item in state.messages
            if item.get("role") == "assistant"
        }
        if normalized and normalized in previous:
            return {
                "quality": "repeated_reply",
                "instruction": (
                    "Реплика дословно повторяет уже сказанное. Не задавай тот же "
                    "вопрос; учти последний ответ и продвинь разговор вперёд."
                ),
            }
        if not self._is_russian_text(message):
            return {
                "quality": "wrong_language",
                "instruction": "Переформулируй живую реплику полностью по-русски.",
            }
        if (
            normalized in {"prodolzhim nulevuyu sessiyu", "продолжим нулевую сессию"}
            and self.missing_fields(draft)
        ):
            return {
                "quality": "empty_progress",
                "instruction": (
                    "Реплика ничего не добавляет. Отреагируй на последний ответ и "
                    "сделай содержательный следующий шаг."
                ),
            }
        return None

    @classmethod
    def _execute_tool_calls(
        cls,
        draft: SessionZeroInterviewDraft,
        calls: list[SessionZeroAgentToolCall],
        *,
        explicit_correction: bool,
    ) -> tuple[SessionZeroInterviewDraft, bool]:
        merged = draft
        finalize_requested = False
        for call in calls:
            if call.name == "update_session_zero" and call.patch is not None:
                merged = cls._apply_patch(
                    merged,
                    call.patch,
                    explicit_correction=explicit_correction,
                )
            elif call.name == "finalize_session_zero":
                finalize_requested = True
        return merged, finalize_requested

    @classmethod
    def _apply_patch(
        cls,
        previous: SessionZeroInterviewDraft,
        patch: SessionZeroInterviewPatch,
        *,
        allowed_topics: list[str] | None = None,
        explicit_correction: bool = False,
    ) -> SessionZeroInterviewDraft:
        """Accumulate agent updates without silently rewriting confirmed facts."""
        merged = previous.model_copy(deep=True)
        for section_name in ("world", "character"):
            patch_section = getattr(patch, section_name)
            target_section = getattr(merged, section_name)
            for field_name in patch_section.model_fields_set:
                new_value = getattr(patch_section, field_name)
                if new_value is None:
                    continue
                old_value = getattr(target_section, field_name)
                if isinstance(old_value, list) and isinstance(new_value, list):
                    if field_name == "starter_npcs" and explicit_correction:
                        setattr(target_section, field_name, list(new_value))
                        continue
                    combined = list(old_value)
                    for item in new_value:
                        if item not in combined:
                            combined.append(item)
                    setattr(target_section, field_name, combined)
                    continue
                if (
                    cls._has_value(old_value)
                    and old_value != new_value
                    and not explicit_correction
                ):
                    continue
                setattr(target_section, field_name, new_value)
        return merged

    @classmethod
    def _rate_limit_retry_seconds(cls, error: str) -> float | None:
        return SessionZeroAgent._rate_limit_retry_seconds(error)

    @classmethod
    def is_rate_limited_error(cls, error: Exception) -> bool:
        return cls._rate_limit_retry_seconds(str(error)) is not None

    @classmethod
    def missing_fields(cls, draft: SessionZeroInterviewDraft) -> list[str]:
        world = draft.world
        character = draft.character
        checks = {
            "world.setting_name": cls._text(world.setting_name),
            "world.genre": cls._text(world.genre),
            "world.premise": cls._text(world.premise),
            "world.tone": cls._text(world.tone),
            "world.world_summary": cls._text(world.world_summary),
            "world.play_style": cls._text(world.play_style),
            "world.starting_location_name": cls._text(world.starting_location_name),
            "world.starting_situation": cls._text(world.starting_situation),
            "world.starter_presence_confirmed": world.starter_presence_confirmed,
            "world.boundaries_confirmed": world.boundaries_confirmed,
            "character.name": cls._text(character.name),
            "character.description": cls._text(character.description),
            "character.appearance": cls._text(character.appearance),
            "character.personality": cls._text(character.personality),
            "character.values": bool(character.values),
            "character.fears": bool(character.fears),
            "character.desires": bool(character.desires),
            "character.voice": cls._text(character.voice),
            "character.speech_patterns": cls._text(character.speech_patterns),
            "character.biography": cls._text(character.biography),
            "character.capabilities": bool(character.capabilities),
            "character.limitations": bool(character.limitations),
            "character.first_goal": cls._text(character.first_goal),
        }
        return [name for name, ready in checks.items() if not ready]

    @classmethod
    def summary(cls, draft: SessionZeroInterviewDraft) -> str:
        world = draft.world
        character = draft.character
        starter_people = [
            npc.name or npc.role for npc in world.starter_npcs if npc.present_at_start
        ]
        return "\n".join(
            [
                f"Мир: {world.world_summary or world.setting_name or '—'}",
                f"Жанр и тон: {world.genre or '—'}; {world.tone or '—'}",
                f"Игра: {world.premise or '—'}",
                f"Стиль: {world.play_style or '—'}",
                f"Границы: {'; '.join(world.boundaries) or 'дополнительных нет'}",
                f"Герой: {character.name or '—'} — {character.description or '—'}",
                f"Внешность: {character.appearance or '—'}",
                f"Характер: {character.personality or '—'}",
                f"Ценности: {'; '.join(character.values) or '—'}",
                f"Страхи: {'; '.join(character.fears) or '—'}",
                f"Желания: {'; '.join(character.desires) or '—'}",
                f"Речь: {character.voice or '—'}; {character.speech_patterns or '—'}",
                f"Прошлое: {character.biography or '—'}",
                f"Сильные стороны: {'; '.join(character.capabilities) or '—'}",
                f"Ограничения: {'; '.join(character.limitations) or '—'}",
                f"Первая цель: {character.first_goal or '—'}",
                f"Старт: {world.starting_location_name or '—'} — "
                f"{world.starting_situation or '—'}",
                f"Кто уже в стартовой сцене: {'; '.join(starter_people) or 'никого кроме героя'}",
            ]
        )

    async def finalize(self, campaign_id: UUID):
        state = await self.get_state(campaign_id)
        if state.pending_user_message:
            raise ValueError("The last answer has not been processed yet")
        missing = self.missing_fields(state.draft)
        if missing:
            raise SessionZeroInterviewIncompleteError(missing)

        world = state.draft.world
        character = state.draft.character
        try:
            location = await self._locations.create(
                campaign_id,
                LocationCreate(
                    canonical_name=world.starting_location_name,
                    description=world.starting_situation,
                    atmosphere=world.tone,
                    custom_fields={"source": "session_zero_agent"},
                ),
            )
            hero = await self._entities.create_character(
                campaign_id,
                CharacterCreate(
                    canonical_name=character.name,
                    description=character.description,
                    appearance=character.appearance,
                    personality=character.personality,
                    values=character.values,
                    fears=character.fears,
                    desires=character.desires,
                    voice=character.voice,
                    speech_patterns=character.speech_patterns,
                    biography=character.biography,
                    current_location_id=location.id,
                    current_intentions=[character.first_goal],
                    custom_fields={
                        "capabilities": character.capabilities,
                        "limitations": character.limitations,
                        "source": "session_zero_agent",
                    },
                ),
            )
            await self._goals.create(
                hero.id,
                GoalCreate(description=character.first_goal, priority=100),
            )
            setup = await self._session_zero.get(campaign_id)
            custom = dict(setup.custom_fields)
            custom[self.STATE_KEY] = state.model_dump(mode="json")
            await self._session_zero.update(
                campaign_id,
                SessionZeroUpdate(
                    setting_name=world.setting_name,
                    genre=world.genre,
                    premise=world.premise,
                    tone=world.tone,
                    themes=world.themes,
                    boundaries=world.boundaries,
                    boundaries_confirmed=world.boundaries_confirmed,
                    rules_system=world.rules_system,
                    world_summary=world.world_summary,
                    starting_situation=world.starting_situation,
                    starting_location_id=location.id,
                    starting_scene_title=(
                        world.starting_scene_title
                        or f"Начало: {world.starting_location_name}"
                    ),
                    play_style=world.play_style,
                    content_rating=world.content_rating,
                    narrative_style=world.narrative_style,
                    player_character_id=hero.id,
                    custom_fields=custom,
                ),
            )
            completed = await self._session_zero.complete(campaign_id)
            await self._session.commit()
            return completed
        except Exception:
            await self._session.rollback()
            raise

    async def _save_state(
        self,
        campaign_id: UUID,
        state: SessionZeroInterviewState,
        *,
        commit: bool,
    ) -> None:
        setup = await self._session_zero.get(campaign_id)
        custom = dict(setup.custom_fields)
        custom[self.STATE_KEY] = state.model_dump(mode="json")
        await self._session_zero.update(
            campaign_id,
            SessionZeroUpdate(custom_fields=custom),
        )
        if commit:
            await self._session.commit()

    @classmethod
    def _has_value(cls, value: object) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, list):
            return bool(value)
        return bool(cls._text(value))

    @staticmethod
    def _text(value: object) -> str:
        return " ".join(str(value or "").split())

    @staticmethod
    def _normalize_text(value: str) -> str:
        normalized = value.casefold().replace("ё", "е")
        normalized = re.sub(r"[^a-zа-я0-9]+", " ", normalized)
        return " ".join(normalized.split())

    @classmethod
    def _is_explicit_correction(cls, value: str) -> bool:
        folded = cls._normalize_text(value)
        return any(marker in folded for marker in cls.CORRECTION_MARKERS)

    @classmethod
    def _is_russian_text(cls, value: str) -> bool:
        cyrillic = sum(
            "а" <= char.casefold() <= "я" or char in "ёЁ" for char in value
        )
        latin = sum("a" <= char.casefold() <= "z" for char in value)
        return cyrillic >= 6 and cyrillic >= latin
