from __future__ import annotations

import asyncio
import json
import re
from typing import ClassVar
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
    SessionZeroInterviewDecision,
    SessionZeroInterviewDraft,
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


class SessionZeroInterviewService:
    """LLM-led conversation that feeds the authoritative session-zero service.

    The model extracts campaign intent, while deterministic guards preserve accepted
    details, keep meta-commands out of the campaign draft, and prevent stalled or
    repeated questions. Materialization still happens once through
    SessionZeroService.complete().
    """

    STATE_KEY = "session_zero_interview"
    MAX_HISTORY_MESSAGES = 6
    MAX_RESPONSE_TOKENS = 1200
    MAX_RATE_LIMIT_WAIT_SECONDS = 8.0
    OPENING_MESSAGE = (
        "Во что тебе хочется сыграть именно сейчас? Можно начать с мира, жанра, "
        "героя или просто с ощущения, которое хочется получить от кампании."
    )
    SYSTEM_PROMPT = """[PERSONAL DM — НУЛЕВАЯ СЕССИЯ]
Ты ведёшь естественную нулевую сессию для одного игрока.

Всегда отвечай только по-русски. Английские названия сеттингов, систем и имена можно
сохранять как собственные названия, но весь разговор с игроком должен быть на русском.
Просьбы вроде «пиши по-русски», замечания «я уже ответил» и недовольство повтором — это
мета-команды общения, а не факты о мире или персонаже.

Это не анкета. Внимательно обработай последний ответ, обнови полный структурированный
черновик и задай один наиболее полезный следующий вопрос. Два тесно связанных вопроса
допустимы, только если разделять их неестественно.

Обязательные правила:
- Сохраняй все подтверждённые детали из CURRENT DRAFT, если игрок явно их не исправил.
- Не стирай заполненное поле только потому, что последний ответ был коротким или мета-командой.
- Не задавай снова вопрос, на который игрок уже ответил или который уже отражён в CURRENT DRAFT.
- Если поле перечислено в ПОЛЯ, ПЕРЕДАННЫЕ МАСТЕРУ, выбери конкретное подходящее значение
  самостоятельно уже в текущем snapshot. Не сохраняй заглушки вроде «на усмотрение мастера»
  или «стартовая точка в сеттинге».
- Если игрок передал мастеру выбор старта, придумай конкретные место и ситуацию, связанные с
  сеттингом, героем и его целью. Первые слова, решения и чувства героя всё равно оставь игроку.
- Не трактуй короткое «нет» или «не очень» как автоматическую передачу выбора мастеру:
  учитывай смысл вопроса и при необходимости уточняй.
- Не выдумывай предпочтения, границы, страхи, ценности, биографию, систему правил или эмоции
  героя только ради заполнения поля.
- Различай ценности и страхи, характер и биографию, голос и манеру речи, способности и
  ограничения.
- Не навязывай рейтинг 18+ или свободную систему правил. Сохраняй реальный выбор игрока.
- Узнавай желаемую инициативу игрока и NPC, тон, темп, границы agency и сложность только
  настолько, насколько это уместно для выбранной кампании.
- Если назван готовый сеттинг или система, уточняй верность канону лишь при необходимости.
- boundaries_confirmed может стать true только после явного указания границ или фразы, что
  дополнительных границ нет.
- ready_to_finalize может быть true только когда все REQUIRED FIELD поддержаны диалогом.
- Возвращай полный snapshot, а не patch. Возвращай только JSON.
- question_topics должен содержать точные имена полей REQUIRED FIELD, о которых задаётся
  следующий вопрос. Если вопроса нет, верни пустой список.

REQUIRED WORLD FIELDS:
world.setting_name, world.genre, world.premise, world.tone, world.world_summary,
world.play_style, world.starting_location_name, world.starting_situation,
world.boundaries_confirmed.

REQUIRED CHARACTER FIELDS:
character.name, character.description, character.appearance, character.personality,
character.values, character.fears, character.desires, character.voice,
character.speech_patterns, character.biography, character.capabilities,
character.limitations, character.first_goal.

assistant_message должен звучать как живой персональный мастер, а не как валидатор формы.
"""

    LANGUAGE_REQUESTS = (
        "пиши по-русски",
        "пиши по русски",
        "только по-русски",
        "только по русски",
        "только на русском",
        "сразу на русском",
        "всегда на русском",
        "русский язык",
    )
    REPEAT_COMPLAINTS = (
        "я уже ответил",
        "я уже отвечал",
        "уже ответил",
        "уже отвечал",
        "я это уже сказал",
        "я уже говорил",
        "не повторяй",
        "ты повторяешь",
        "опять тот же вопрос",
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
    NO_PREFERENCE_ANSWERS: ClassVar[set[str]] = {
        "неважно",
        "без разницы",
        "как хочешь",
        "на твое усмотрение",
        "на твоё усмотрение",
        "на усмотрение мастера",
        "нет особых предпочтений",
        "особых предпочтений нет",
        "выбери сам",
        "решай сам",
    }
    DELEGATABLE_FIELDS: ClassVar[set[str]] = {
        "world.premise",
        "world.tone",
        "world.play_style",
        "world.starting_location_name",
        "world.starting_situation",
    }
    START_FIELDS: ClassVar[set[str]] = {
        "world.starting_location_name",
        "world.starting_situation",
    }
    QUESTION_ORDER: ClassVar[tuple[str, ...]] = (
        "world.setting_name",
        "world.genre",
        "world.world_summary",
        "character.name",
        "character.description",
        "character.appearance",
        "character.personality",
        "character.values",
        "character.fears",
        "character.desires",
        "character.first_goal",
        "character.capabilities",
        "character.limitations",
        "world.premise",
        "world.tone",
        "world.play_style",
        "character.biography",
        "character.voice",
        "character.speech_patterns",
        "world.starting_location_name",
        "world.starting_situation",
        "world.boundaries_confirmed",
    )
    QUESTION_TEXT: ClassVar[dict[str, str]] = {
        "world.setting_name": "Какой мир или сеттинг берём за основу?",
        "world.genre": "Какой жанр должен быть у этой кампании?",
        "world.world_summary": (
            "Какие черты выбранного мира особенно важны для этой кампании?"
        ),
        "world.premise": "Вокруг чего должна строиться кампания?",
        "world.tone": "Какой тон и темп игры тебе сейчас хочется?",
        "world.play_style": (
            "Что должно быть в центре игры: задания, выживание, отношения, "
            "расследования, тактика или что-то другое?"
        ),
        "world.starting_location_name": "В каком конкретном месте начнётся игра?",
        "world.starting_situation": "Что именно происходит в первой сцене?",
        "world.boundaries_confirmed": (
            "Есть ли темы или способы ведения игры, которых точно не должно быть? "
            "Можно просто сказать, что дополнительных границ нет."
        ),
        "character.name": "Как зовут героя?",
        "character.description": "Кто этот герой и чем он занимается?",
        "character.appearance": "Как выглядит герой?",
        "character.personality": "Какой у героя характер и как он обычно себя ведёт?",
        "character.values": "Какие принципы герой не готов переступить?",
        "character.fears": "Чего герой по-настоящему боится?",
        "character.desires": "К чему герой стремится в долгую?",
        "character.first_goal": "Чего герой хочет добиться в самом начале кампании?",
        "character.capabilities": "Что герой уже умеет делать хорошо?",
        "character.limitations": "Какие слабости или ограничения есть у героя?",
        "character.biography": "Что важно знать о прошлом героя?",
        "character.voice": "Как звучит голос героя?",
        "character.speech_patterns": "Как герой обычно говорит с людьми?",
    }
    TOPIC_KEYWORDS: ClassVar[dict[str, tuple[str, ...]]] = {
        "world.setting_name": ("мир", "сеттинг", "вселен", "канон"),
        "world.genre": ("жанр",),
        "world.world_summary": ("черты мира", "особенно важны", "описание мира"),
        "world.premise": ("кампани", "сюжет", "приключ"),
        "world.tone": ("тон", "темп", "атмосфер"),
        "world.play_style": ("центр игры", "важнее от игры", "геймплей"),
        "world.starting_location_name": ("где начать", "место", "локац"),
        "world.starting_situation": ("старт", "ситуац", "первой сцен"),
        "world.boundaries_confirmed": ("границ", "не должно", "не хочется видеть"),
        "character.name": ("как зовут", "имя персонаж", "имя героя"),
        "character.description": ("кто он", "кто герой", "концепц", "занимается"),
        "character.appearance": ("внешност", "выгляд"),
        "character.personality": ("характер", "личност", "ведёт себя", "повед"),
        "character.values": ("ценност", "принцип"),
        "character.fears": ("страх", "боится"),
        "character.desires": ("хочет", "стремится", "желани", "в долгую"),
        "character.voice": ("голос", "звучит"),
        "character.speech_patterns": ("говорит", "манер", "речь"),
        "character.biography": ("прошл", "биограф", "истори"),
        "character.capabilities": ("умеет", "навык", "способност", "сил"),
        "character.limitations": ("слаб", "огранич", "не умеет"),
        "character.first_goal": ("первая цель", "в начале", "добиться"),
    }
    LEGACY_DELEGATION_VALUES: ClassVar[dict[str, set[str]]] = {
        "world.premise": {
            "Герой начинает с личной цели и постепенно вовлекается в события мира."
        },
        "world.tone": {
            "На усмотрение мастера в рамках согласованных тем и границ."
        },
        "world.play_style": {
            "Гибкий стиль с учётом решений игрока и естественных последствий."
        },
        "world.starting_situation": {
            "Мастер выбирает подходящую для сеттинга стартовую ситуацию; первые слова, "
            "решения и чувства героя остаются за игроком."
        },
    }

    def __init__(self, session: AsyncSession):
        self._session = session
        self._session_zero = SessionZeroService(session)
        self._entities = EntityRepository(session)
        self._locations = LocationRepository(session)
        self._goals = GoalRepository(session)
        self._provider = LLMProvider()
        self._router = RoleModelRouter(ProviderConfigRepository(session))

    async def get_state(self, campaign_id: UUID) -> SessionZeroInterviewState:
        setup = await self._session_zero.get(campaign_id)
        raw = setup.custom_fields.get(self.STATE_KEY)
        if not isinstance(raw, dict):
            return SessionZeroInterviewState()
        try:
            state = SessionZeroInterviewState.model_validate(raw)
        except ValueError:
            return SessionZeroInterviewState()
        self._clear_legacy_delegation_placeholders(state)
        return state

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

        direct = await self._handle_control_message(campaign_id, state, clean)
        if direct is not None:
            return direct

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

    async def _handle_control_message(
        self,
        campaign_id: UUID,
        state: SessionZeroInterviewState,
        clean: str,
    ) -> SessionZeroInterviewDecision | None:
        folded = clean.casefold().replace("ё", "е")
        if any(item.replace("ё", "е") in folded for item in self.LANGUAGE_REQUESTS):
            state.response_language = "ru"
            return await self._direct_decision(
                campaign_id,
                state,
                clean,
                "Да. Дальше говорю только по-русски.",
                preferred_topics=state.last_question_topics,
            )

        if any(item.replace("ё", "е") in folded for item in self.REPEAT_COMPLAINTS):
            return await self._direct_decision(
                campaign_id,
                state,
                clean,
                "Да, ты уже ответил. Повторять тот же вопрос не буду.",
                exclude_topics=state.last_question_topics,
            )

        normalized = self._normalize_text(clean)
        delegatable = set(state.last_question_topics) & self.DELEGATABLE_FIELDS
        if normalized in self.NO_PREFERENCE_ANSWERS and delegatable:
            if delegatable & self.START_FIELDS:
                delegatable.update(self.START_FIELDS)
            state.delegated_fields = sorted(
                set(state.delegated_fields) | delegatable
            )
        return None

    async def _direct_decision(
        self,
        campaign_id: UUID,
        state: SessionZeroInterviewState,
        user_message: str,
        prefix: str,
        *,
        preferred_topics: list[str] | None = None,
        exclude_topics: list[str] | None = None,
    ) -> SessionZeroInterviewDecision:
        state.messages.append({"role": "user", "content": user_message})
        missing = self.missing_fields(state.draft)
        current_topics = [
            topic for topic in (preferred_topics or []) if topic in set(missing)
        ]
        if current_topics:
            question, topics = self._question_for_topics(current_topics, state.draft)
        else:
            question, topics = self._next_question(
                state.draft,
                missing,
                exclude_topics=exclude_topics or [],
            )
        ready = not missing
        assistant_message = prefix if ready else f"{prefix} {question}"
        decision = SessionZeroInterviewDecision(
            assistant_message=assistant_message,
            ready_to_finalize=ready,
            draft=state.draft,
            missing_topics=missing,
            question_topics=topics,
            summary=self.summary(state.draft),
        )
        state.messages.append(
            {"role": "assistant", "content": decision.assistant_message}
        )
        state.pending_user_message = None
        state.last_question_topics = topics
        state.last_summary = decision.summary
        await self._save_state(campaign_id, state, commit=True)
        return decision

    @classmethod
    def _rate_limit_retry_after(cls, error: Exception) -> float | None:
        text = str(error)
        folded = text.casefold()
        if "429" not in text and "rate_limit" not in folded:
            return None
        match = re.search(
            r"(?:try again in|retry after)\s*(\d+(?:\.\d+)?)\s*s",
            text,
            flags=re.IGNORECASE,
        )
        return float(match.group(1)) if match else 1.0

    @classmethod
    def is_rate_limited_error(cls, error: Exception) -> bool:
        return cls._rate_limit_retry_after(error) is not None

    async def _generate_interview_json(self, selection, messages) -> dict:
        async def generate() -> dict:
            return await self._router.generate_json(
                self._provider,
                selection,
                messages,
                max_tokens=self.MAX_RESPONSE_TOKENS,
                temperature=0.25,
                response_model=SessionZeroInterviewDecision,
            )

        try:
            return await generate()
        except LLMProviderError as exc:
            retry_after = self._rate_limit_retry_after(exc)
            if retry_after is None:
                raise
            delay = min(
                max(retry_after, 0.2) + 0.25,
                self.MAX_RATE_LIMIT_WAIT_SECONDS,
            )
            await asyncio.sleep(delay)
            return await generate()

    async def _continue_pending(
        self,
        campaign_id: UUID,
        state: SessionZeroInterviewState,
    ) -> SessionZeroInterviewDecision:
        selection = await self._router.resolve(campaign_id, ModelRole.SESSION_ZERO)
        if selection is None:
            raise LLMProviderError(
                "No LLM provider is configured for this campaign"
            )
        current = json.dumps(
            state.draft.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        missing_before = self.missing_fields(state.draft)
        messages = [
            ChatMessage(
                role="system",
                content=(
                    f"{self.SYSTEM_PROMPT}\n\n"
                    f"[ЯЗЫК ОТВЕТА]\n{state.response_language}\n\n"
                    f"[ПОСЛЕДНИЕ ТЕМЫ ВОПРОСА]\n"
                    f"{json.dumps(state.last_question_topics, ensure_ascii=False)}\n\n"
                    f"[ПОЛЯ, ПЕРЕДАННЫЕ МАСТЕРУ]\n"
                    f"{json.dumps(state.delegated_fields, ensure_ascii=False)}\n\n"
                    f"[ЕЩЁ НЕ ХВАТАЕТ]\n"
                    f"{json.dumps(missing_before, ensure_ascii=False)}\n\n"
                    f"[CURRENT DRAFT]\n{current}"
                ),
            ),
            *[
                ChatMessage(role=item["role"], content=item["content"])
                for item in state.messages[-self.MAX_HISTORY_MESSAGES :]
                if item.get("role") in {"user", "assistant"}
                and item.get("content")
            ],
        ]
        data = await self._generate_interview_json(selection, messages)
        decision = SessionZeroInterviewDecision.model_validate(data)
        latest_user_message = state.pending_user_message or ""
        merged = self._merge_drafts(
            state.draft,
            decision.draft,
            allowed_topics=state.last_question_topics,
            explicit_correction=self._is_explicit_correction(latest_user_message),
        )
        missing = self.missing_fields(merged)
        decision.draft = merged
        decision.missing_topics = missing
        decision.ready_to_finalize = decision.ready_to_finalize and not missing
        decision.assistant_message, decision.question_topics = (
            self._guard_assistant_message(
                decision.assistant_message,
                decision.question_topics,
                state,
                merged,
                missing,
                decision.ready_to_finalize,
            )
        )
        state.draft = merged
        state.messages.append(
            {"role": "assistant", "content": decision.assistant_message}
        )
        state.pending_user_message = None
        state.last_question_topics = decision.question_topics
        state.last_summary = decision.summary or self.summary(decision.draft)
        await self._save_state(campaign_id, state, commit=True)
        return decision

    def _guard_assistant_message(
        self,
        message: str,
        declared_topics: list[str],
        state: SessionZeroInterviewState,
        draft: SessionZeroInterviewDraft,
        missing: list[str],
        ready: bool,
    ) -> tuple[str, list[str]]:
        clean_message = " ".join(message.split())
        if ready:
            if self._is_russian_text(clean_message):
                return clean_message, []
            return "Основа кампании собрана. Проверь итоговую сводку перед стартом.", []

        valid_topics = [topic for topic in declared_topics if topic in missing]
        if not valid_topics:
            valid_topics = self._infer_topics(clean_message, missing)

        previous_messages = {
            self._normalize_text(item.get("content", ""))
            for item in state.messages
            if item.get("role") == "assistant"
        }
        repeated = self._normalize_text(clean_message) in previous_messages
        wrong_language = not self._is_russian_text(clean_message)
        asks_only_filled = bool(declared_topics) and not valid_topics
        mentioned_topics = set(self._infer_topics(clean_message, list(self.QUESTION_ORDER)))
        mentions_filled_topics = bool(mentioned_topics - set(missing))

        if wrong_language and valid_topics:
            return self._question_for_topics(valid_topics, draft)
        if repeated or asks_only_filled or not valid_topics or mentions_filled_topics:
            return self._next_question(
                draft,
                missing,
                exclude_topics=state.last_question_topics,
            )
        return clean_message, valid_topics

    @classmethod
    def _merge_drafts(
        cls,
        previous: SessionZeroInterviewDraft,
        proposed: SessionZeroInterviewDraft,
        *,
        allowed_topics: list[str] | None = None,
        explicit_correction: bool = False,
    ) -> SessionZeroInterviewDraft:
        merged = proposed.model_copy(deep=True)
        allowed = set(allowed_topics or [])
        for section_name in ("world", "character"):
            old_section = getattr(previous, section_name)
            new_section = getattr(merged, section_name)
            for field_name in old_section.__class__.model_fields:
                topic = f"{section_name}.{field_name}"
                old_value = getattr(old_section, field_name)
                new_value = getattr(new_section, field_name)
                if cls._has_value(old_value) and not cls._has_value(new_value):
                    setattr(new_section, field_name, old_value)
                    continue
                if (
                    cls._has_value(old_value)
                    and cls._has_value(new_value)
                    and old_value != new_value
                    and topic not in allowed
                    and not explicit_correction
                ):
                    setattr(new_section, field_name, old_value)
        return merged

    @classmethod
    def _next_question(
        cls,
        draft: SessionZeroInterviewDraft,
        missing: list[str],
        *,
        exclude_topics: list[str],
    ) -> tuple[str, list[str]]:
        if not missing:
            return "", []
        missing_set = set(missing)
        excluded = set(exclude_topics)
        for topic in cls.QUESTION_ORDER:
            if topic in missing_set and topic not in excluded:
                return cls._question_for_topics([topic], draft)
        for topic in cls.QUESTION_ORDER:
            if topic in missing_set:
                return cls._question_for_topics([topic], draft)
        return "Расскажи, что ещё важно закрепить перед началом игры?", missing[:1]

    @classmethod
    def _question_for_topics(
        cls,
        topics: list[str],
        draft: SessionZeroInterviewDraft,
    ) -> tuple[str, list[str]]:
        topic_set = set(topics)
        ordered = [topic for topic in cls.QUESTION_ORDER if topic in topic_set]
        if not ordered:
            return "Расскажи, что ещё важно закрепить перед началом игры?", topics[:1]
        if set(ordered) >= cls.START_FIELDS:
            return (
                "С какой конкретной ситуации начать кампанию? Можно назвать место и момент "
                "самому или прямо отдать выбор старта мастеру.",
                [
                    "world.starting_location_name",
                    "world.starting_situation",
                ],
            )
        topic = ordered[0]
        question = cls.QUESTION_TEXT.get(
            topic,
            "Расскажи, что ещё важно закрепить перед началом игры?",
        )
        return cls._personalize_question(question, draft), [topic]

    @staticmethod
    def _personalize_question(
        question: str,
        draft: SessionZeroInterviewDraft,
    ) -> str:
        hero_name = " ".join((draft.character.name or "").split())
        if hero_name and "герой" in question.casefold():
            return re.sub(r"\bгерой\b", hero_name, question, flags=re.IGNORECASE)
        return question

    @classmethod
    def _infer_topics(cls, message: str, candidates: list[str]) -> list[str]:
        folded = message.casefold().replace("ё", "е")
        inferred: list[str] = []
        for topic in candidates:
            keywords = cls.TOPIC_KEYWORDS.get(topic, ())
            if any(keyword.replace("ё", "е") in folded for keyword in keywords):
                inferred.append(topic)
        return inferred

    @classmethod
    def _is_russian_text(cls, value: str) -> bool:
        cyrillic = sum("а" <= char.casefold() <= "я" or char in "ёЁ" for char in value)
        latin = sum("a" <= char.casefold() <= "z" for char in value)
        return cyrillic >= 6 and cyrillic >= latin

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
    def _has_value(cls, value: object) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, list):
            return bool(value)
        return bool(cls._text(value))

    @classmethod
    def _clear_legacy_delegation_placeholders(
        cls,
        state: SessionZeroInterviewState,
    ) -> None:
        delegated = set(state.delegated_fields)
        world = state.draft.world
        for topic, values in cls.LEGACY_DELEGATION_VALUES.items():
            if topic not in delegated:
                continue
            field_name = topic.split(".", maxsplit=1)[1]
            if getattr(world, field_name) in values:
                setattr(world, field_name, None)
        if (
            "world.starting_location_name" in delegated
            and cls._text(world.starting_location_name).startswith("Стартовая точка в ")
        ):
            world.starting_location_name = None

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
                    custom_fields={"source": "session_zero_interview"},
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
                        "source": "session_zero_interview",
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
            "world.starting_location_name": cls._text(
                world.starting_location_name
            ),
            "world.starting_situation": cls._text(world.starting_situation),
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
        return "\n".join(
            [
                f"Мир: {world.world_summary or world.setting_name or '—'}",
                f"Жанр и тон: {world.genre or '—'}; {world.tone or '—'}",
                f"Игра: {world.premise or '—'}",
                f"Стиль: {world.play_style or '—'}",
                f"Границы: {'; '.join(world.boundaries) or 'дополнительных нет'}",
                f"Герой: {character.name or '—'} — {character.description or '—'}",
                f"Характер: {character.personality or '—'}",
                f"Ценности: {'; '.join(character.values) or '—'}",
                f"Страхи: {'; '.join(character.fears) or '—'}",
                f"Желания: {'; '.join(character.desires) or '—'}",
                f"Сильные стороны: {'; '.join(character.capabilities) or '—'}",
                f"Ограничения: {'; '.join(character.limitations) or '—'}",
                f"Первая цель: {character.first_goal or '—'}",
                f"Старт: {world.starting_location_name or '—'} — "
                f"{world.starting_situation or '—'}",
            ]
        )

    @staticmethod
    def _text(value: object) -> str:
        return " ".join(str(value or "").split())
