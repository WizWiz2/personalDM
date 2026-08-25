"""Compatibility boundary and resilience layer for the Session Zero agent.

The core conversational implementation lives in ``session_zero_agent``. This module
keeps the historic import path used by CLI and API code while adding transport,
conversation-quality and autonomous-start guards that remain provider-neutral.
"""

from __future__ import annotations

import asyncio
import json
from uuid import UUID

from app.models.session_zero_interview import (
    SessionZeroInterviewDecision,
    SessionZeroInterviewDraft,
    SessionZeroInterviewModelDecision,
    SessionZeroInterviewPatch,
    SessionZeroInterviewState,
)
from app.models.turn import ChatMessage
from app.providers.llm_provider import LLMProviderError
from app.services.role_model_router import ModelRole, RoleModelRouter
from app.services.session_zero_agent import (
    SessionZeroAgent as _BaseSessionZeroAgent,
)
from app.services.session_zero_agent import (
    SessionZeroInterviewIncompleteError,
)
from app.services.session_zero_agent import (
    SessionZeroInterviewService as _BaseSessionZeroInterviewService,
)


class SessionZeroAgent(_BaseSessionZeroAgent):
    """Session Zero agent that owns both the conversation and the start decision."""

    SYSTEM_PROMPT = _BaseSessionZeroAgent.SYSTEM_PROMPT + """

АВТОНОМНОЕ РЕШЕНИЕ О СТАРТЕ
- Ты сам решаешь, когда набрана достаточная критическая масса информации. Не жди, пока
  будут заполнены все поля карточки: это не анкета и не чек-лист.
- Обычно достаточно понять общий тип мира, базовый концепт героя и зацепку, из которой
  можно сделать первую сцену. Необязательные черты раскрывай уже во время игры.
- Если игрок прямо предлагает начать, говорит «погнали», «норм», «не знаю», «выбери
  сам» или несколько раз не может добавить деталей, не мучай его вопросами. Придумай
  безопасные недостающие детали, сохрани их через update_session_zero и вызови
  finalize_session_zero в том же ответе.
- Если игрок почти ничего не знает, сам собери один яркий, но не слишком специфичный
  вариант мира, героя, первой цели и стартовой ситуации. Можно коротко предложить его;
  при отсутствии возражений сразу начинай.
- Не начинай разыгрывать действия и результаты внутри нулевой сессии. Когда готова
  первая игровая сцена, вызови finalize_session_zero: после этого управление перейдёт
  обычному Рассказчику.
- В финальной реплике не задавай новый вопрос. Коротко объяви переход к первой сцене.

АНТИ-АНКЕТА И ЦЕЛОСТНОСТЬ ОТВЕТОВ
- Перед тем как перейти дальше, обязательно сохрани через update_session_zero смысл
  последнего ответа игрока. Нельзя принять ответ, забыть записать его и позже спросить
  то же самое другими словами.
- question_topics указывай только для вопроса, который реально задаёшь в текущей
  реплике. Не указывай уже заполненные темы.
- Не проводи отдельную цепочку «ценности → страхи → желания → речь → слабости».
  После одного-двух узких уточнений синтезируй безопасные детали из уже описанного
  концепта, предложи интерпретацию или переходи к старту.
- Не спрашивай отдельно и повторно то, что уже прямо следует из ответа игрока. Например,
  «хочет найти кибердеку получше» уже является первой целью, а «немногословно» уже
  описывает манеру речи.
- assistant_message обязан содержать обычную русскую реплику, а не пустую строку,
  пробелы или техническую заглушку.
"""

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
        critical_gaps = SessionZeroInterviewService.missing_fields(state.draft)
        system_content = (
            f"{self.SYSTEM_PROMPT}\n\n"
            f"[ТЕКУЩАЯ КАРТОЧКА — ТОЛЬКО ДЛЯ ЧТЕНИЯ]\n{current}\n\n"
            "[ТЕХНИЧЕСКИЙ МИНИМУМ ДЛЯ СОЗДАНИЯ ГЕРОЯ И СЦЕНЫ]\n"
            f"{json.dumps(critical_gaps, ensure_ascii=False)}\n"
            "Это не список вопросов игроку. Если эти детали отсутствуют, сначала "
            "попробуй разумно придумать их сам и сохранить инструментом."
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
                        + "\nСам реши, как естественно продолжить. Если данных уже "
                        "достаточно, заполни технические пробелы сам и заверши нулевую "
                        "сессию вместо нового вопроса."
                    ),
                )
            )
        data = await self._generate(selection, messages)
        return SessionZeroInterviewModelDecision.model_validate(data)


class SessionZeroInterviewService(_BaseSessionZeroInterviewService):
    """Resilient tool executor for the conversational Session Zero agent."""

    MAX_QUALITY_REPAIRS = 2
    MAX_START_MATERIALIZER_ATTEMPTS = 2
    START_MATERIALIZER_TOKENS = 700
    SAFE_FALLBACK_MESSAGE = (
        "Понял. Недостающие детали я возьму на себя и подготовлю стартовую сцену."
    )
    START_MATERIALIZER_PROMPT = """[SESSION ZERO START MATERIALIZER]
Ты не ведёшь разговор и не пишешь художественную сцену. Твоя единственная задача —
достроить технический минимум уже согласованной нулевой сессии, чтобы приложение могло
создать героя, локацию и первую сцену.

Правила:
- Верни только SessionZeroInterviewPatch по заданной JSON-схеме.
- Заполняй только перечисленные технические пробелы; существующие значения не меняй.
- Используй конкретные русские значения, которые логично следуют из карточки и диалога.
- Не придумывай новые личные границы игрока и не меняй подтверждённые решения.
- Не задавай вопросов и не вызывай инструменты.
- starting_location_name — короткое название реального места старта.
- starting_situation — конкретная ситуация, которая немедленно даёт игроку действие.
- character.description — цельный концепт героя, а не список полей.
- character.first_goal — практическая ближайшая цель героя.
- Если известен сеттинг/жанр, сохраняй его смысл и не подменяй другим миром.
"""
    NARROW_CHARACTER_TOPICS = frozenset(
        {
            "character.personality",
            "character.values",
            "character.fears",
            "character.desires",
            "character.voice",
            "character.speech_patterns",
            "character.capabilities",
            "character.limitations",
            "character.first_goal",
        }
    )
    DELEGATION_MARKERS = (
        "не знаю",
        "не могу сказать",
        "без разницы",
        "реши сам",
        "выбери сам",
        "на твой выбор",
        "как хочешь",
        "на усмотрение мастера",
        "нет таких",
    )
    START_REQUEST_MARKERS = (
        "начинаем игру",
        "начать игру",
        "начинай игру",
        "начинай прямо",
        "начинай сейчас",
        "начинай как можно скорее",
        "давай начинай",
        "давай играть",
        "можно начинать",
        "хочу начать",
        "погнали",
        "стартуем",
        "запускай",
        "без новых вопросов",
        "и что происходит",
    )
    START_CLAIM_MARKERS = (
        "давай начнем",
        "давай начнём",
        "начнем ",
        "начнём ",
        "начинаем игру",
        "игра начинается",
        "приключение начинается",
        "начинаем с первой сцены",
        "первая сцена начинается",
        "переходим к первой сцене",
        "переходим к игре",
        "приступим к игре",
        "мы находимся",
    )
    TOPIC_PATTERNS = (
        (
            "world.boundaries_confirmed",
            ("точно не должно", "дополнительных границ", "темы которых", "стоп темы"),
        ),
        (
            "world.starting_location_name",
            ("где нач", "место начала", "стартовая локац", "в какой локац"),
        ),
        (
            "world.starting_situation",
            ("с какой ситуац", "что происходит в начале", "начальная ситуац"),
        ),
        (
            "character.first_goal",
            ("первая цель", "в самом начале кампании", "добиться в начале", "первым делом"),
        ),
        ("character.capabilities", ("умеет делать", "сильные стороны", "что умеет")),
        ("character.limitations", ("слабости", "ограничения", "что ему мешает")),
        ("character.biography", ("прошл", "биограф", "что с ним произошло", "как вырос")),
        ("character.personality", ("характер", "личность", "какой он по натуре")),
        ("character.values", ("ценност", "важнее всего", "принцип")),
        ("character.fears", ("боится", "страх", "опасается больше всего")),
        ("character.desires", ("желани", "мечта", "чего хочет от жизни")),
        (
            "character.speech_patterns",
            ("как говорит", "как общается", "манера речи", "немногослов"),
        ),
        ("character.appearance", ("как выглядит", "внешност", "одет")),
        ("character.description", ("кто такой", "чем занимается", "концепт героя")),
        ("world.tone", ("тон игры", "настроение кампании", "насколько мрач")),
        ("world.play_style", ("стиль игры", "что будет в центре игры", "как будем играть")),
        ("world.premise", ("о чем кампания", "основа кампании", "главный конфликт")),
    )

    def __init__(self, session) -> None:
        super().__init__(session)
        self._agent = SessionZeroAgent(self._provider, self._router)

    @classmethod
    def missing_fields(cls, draft: SessionZeroInterviewDraft) -> list[str]:
        """Return only fields that are technically required to materialize play.

        The agent, not this method, decides whether the conversation is creatively
        sufficient. Optional card details may remain empty and emerge during play.
        """
        world = draft.world
        character = draft.character
        missing: list[str] = []
        if not any(
            cls._text(value)
            for value in (world.setting_name, world.genre, world.world_summary)
        ):
            missing.append("world.setting_or_genre")
        checks = {
            "world.starting_location_name": cls._text(world.starting_location_name),
            "world.starting_situation": cls._text(world.starting_situation),
            "character.name": cls._text(character.name),
            "character.description": cls._text(character.description),
            "character.first_goal": cls._text(character.first_goal),
        }
        missing.extend(name for name, ready in checks.items() if not ready)
        return missing

    async def _materialize_start(
        self,
        selection,
        state: SessionZeroInterviewState,
        draft: SessionZeroInterviewDraft,
        latest_user_message: str,
    ) -> SessionZeroInterviewDraft:
        """Fill technical start gaps with a schema-constrained call, then safe defaults."""
        merged = draft
        for _ in range(self.MAX_START_MATERIALIZER_ATTEMPTS):
            missing = self.missing_fields(merged)
            if not missing:
                return merged
            current = json.dumps(
                merged.model_dump(mode="json"),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            transcript = "\n".join(
                f"{item.get('role')}: {item.get('content')}"
                for item in state.messages[-8:]
                if item.get("role") in {"user", "assistant"}
                and item.get("content")
            )
            messages = [
                ChatMessage(
                    role="system",
                    content=(
                        f"{self.START_MATERIALIZER_PROMPT}\n\n"
                        f"[ТЕКУЩАЯ КАРТОЧКА]\n{current}\n\n"
                        "[ТЕХНИЧЕСКИЕ ПРОБЕЛЫ]\n"
                        f"{json.dumps(missing, ensure_ascii=False)}\n\n"
                        "[ПОСЛЕДНИЙ ДИАЛОГ]\n"
                        f"{transcript or '[пусто]'}"
                    ),
                ),
                ChatMessage(
                    role="user",
                    content=(
                        "Игрок и/или мастер уже решили переходить к игре. "
                        f"Последний ответ игрока: {latest_user_message or '[нет]'}. "
                        "Дострой только технические пробелы и верни patch."
                    ),
                ),
            ]
            try:
                data = await self._router.generate_json(
                    self._provider,
                    selection,
                    messages,
                    max_tokens=self.START_MATERIALIZER_TOKENS,
                    temperature=0.1,
                    response_model=SessionZeroInterviewPatch,
                )
            except LLMProviderError:
                break
            patch = SessionZeroInterviewPatch.model_validate(data)
            merged = self._apply_patch(
                merged,
                patch,
                explicit_correction=False,
            )
        return self._technical_start_defaults(merged)

    @classmethod
    def _technical_start_defaults(
        cls,
        draft: SessionZeroInterviewDraft,
    ) -> SessionZeroInterviewDraft:
        """Last-resort materialization without inventing personal player preferences."""
        merged = draft.model_copy(deep=True)
        world = merged.world
        character = merged.character

        if not any(
            cls._text(value)
            for value in (world.setting_name, world.genre, world.world_summary)
        ):
            world.world_summary = (
                cls._text(world.premise)
                or "Мир приключения, согласованный в нулевой сессии."
            )

        if not cls._text(character.name):
            character.name = "Герой"

        if not cls._text(character.description):
            description_parts = [
                cls._text(character.personality),
                cls._text(character.biography),
                ", ".join(character.capabilities).strip(),
            ]
            character.description = next(
                (part for part in description_parts if part),
                "Главный герой кампании.",
            )

        if not cls._text(character.first_goal):
            character.first_goal = (
                cls._text(world.starting_situation)
                or "Разобраться с первой зацепкой и определить следующий шаг."
            )

        if not cls._text(world.starting_location_name):
            world.starting_location_name = "Стартовая локация"

        if not cls._text(world.starting_situation):
            world.starting_situation = (
                f"{character.name} сталкивается с первой зацепкой, связанной с целью: "
                f"{character.first_goal}."
            )

        return merged

    async def _continue_pending(
        self,
        campaign_id: UUID,
        state: SessionZeroInterviewState,
    ) -> SessionZeroInterviewDecision:
        selection = await self._router.resolve(campaign_id, ModelRole.SESSION_ZERO)
        if selection is None:
            raise LLMProviderError("No LLM provider is configured for this campaign")

        latest_user_message = state.pending_user_message or ""
        if self._is_delegation(latest_user_message):
            for topic in state.last_question_topics:
                if topic not in state.delegated_fields:
                    state.delegated_fields.append(topic)

        explicit_correction = self._is_explicit_correction(latest_user_message)
        merged = state.draft
        finalize_requested = False
        model_decision = await self._agent.respond(selection, state)
        unresolved_feedback: dict | None = None

        for attempt in range(self.MAX_QUALITY_REPAIRS + 1):
            merged, requested_now = self._execute_tool_calls(
                merged,
                model_decision.tool_calls,
                explicit_correction=explicit_correction,
            )
            finalize_requested = requested_now

            feedback = self._quality_feedback(
                model_decision,
                state,
                merged,
                latest_user_message=latest_user_message,
                explicit_correction=explicit_correction,
            )
            if requested_now:
                missing = self.missing_fields(merged)
                if missing:
                    finalize_feedback = {
                        "tool": "finalize_session_zero",
                        "ok": False,
                        "technical_missing_fields": missing,
                        "instruction": (
                            "Это только технические поля для создания объектов, а не "
                            "повод продолжать анкету. Придумай безопасные значения сам, "
                            "сохрани их через update_session_zero и повтори "
                            "finalize_session_zero в этом же ходе. Спрашивай игрока "
                            "только при явном конфликте с его словами."
                        ),
                    }
                    feedback = self._combine_feedback(feedback, finalize_feedback)

            if feedback is None:
                unresolved_feedback = None
                break

            unresolved_feedback = feedback
            if attempt >= self.MAX_QUALITY_REPAIRS:
                break

            state.draft = merged
            model_decision = await self._agent.respond(
                selection,
                state,
                feedback=feedback,
            )

        start_signal = (
            finalize_requested
            or self._start_requested(latest_user_message)
            or self._assistant_claims_start(model_decision.assistant_message)
        )
        if start_signal:
            if self.missing_fields(merged):
                merged = await self._materialize_start(
                    selection,
                    state,
                    merged,
                    latest_user_message,
                )
            if not self.missing_fields(merged):
                # Meaning is neural; completion of the technical transition is not.
                # Existing CLI/API callers will now invoke `finalize()` reliably.
                finalize_requested = True
                unresolved_feedback = None

        missing = self.missing_fields(merged)
        ready = finalize_requested and not missing
        assistant_message = self._safe_assistant_message(
            model_decision.assistant_message,
            ready=ready,
            quality_failed=unresolved_feedback is not None,
        )
        question_topics = self._effective_question_topics(model_decision)
        if unresolved_feedback is not None or ready:
            question_topics = []

        decision = SessionZeroInterviewDecision(
            assistant_message=assistant_message,
            ready_to_finalize=ready,
            draft=merged,
            missing_topics=missing,
            question_topics=question_topics,
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
        *,
        latest_user_message: str,
        explicit_correction: bool,
    ) -> dict | None:
        message = self._text(decision.assistant_message)
        if not message:
            return {
                "quality": "empty_reply",
                "instruction": (
                    "Верни содержательную русскую реплику. Пустая строка и пробелы "
                    "недопустимы."
                ),
            }

        base_feedback = super()._quality_feedback(decision, state, draft)
        if base_feedback is not None:
            return base_feedback

        if not decision.ready_to_finalize and (
            self._start_requested(latest_user_message)
            or self._assistant_claims_start(message)
        ):
            return {
                "quality": "start_announced_without_finalize",
                "player_message": latest_user_message,
                "technical_missing_fields": self.missing_fields(draft),
                "instruction": (
                    "Игрок попросил начать или ты уже объявил начало игры, но не "
                    "вызвал finalize_session_zero. Не разыгрывай сцену внутри интервью. "
                    "Сам дострой безопасные технические детали через "
                    "update_session_zero и вызови finalize_session_zero в этом же ходе."
                ),
            }

        previous_topics = list(dict.fromkeys(state.last_question_topics))
        unrecorded = [
            topic
            for topic in previous_topics
            if topic not in state.delegated_fields
            and not self._topic_has_value(draft, topic)
        ]
        if unrecorded and self._is_substantive_answer(latest_user_message):
            return {
                "quality": "unrecorded_player_answer",
                "topics": unrecorded,
                "player_answer": latest_user_message,
                "instruction": (
                    "Ты перешёл дальше, не сохранив ответ игрока на предыдущий "
                    "вопрос. Сначала вызови update_session_zero и запиши смысл ответа; "
                    "затем либо естественно продвинь разговор, либо заверши нулевую "
                    "сессию, если критической массы уже достаточно."
                ),
            }

        current_topics = self._effective_question_topics(decision)
        if not explicit_correction:
            already_filled = [
                topic for topic in current_topics if self._topic_has_value(draft, topic)
            ]
            if already_filled:
                return {
                    "quality": "question_already_answered",
                    "topics": already_filled,
                    "instruction": (
                        "Ты снова спрашиваешь уже заполненную характеристику. Учти "
                        "сохранённое значение, не переспрашивай его другими словами и "
                        "либо двигайся к стартовой сцене, либо заверши нулевую сессию."
                    ),
                }

            delegated_repeat = [
                topic for topic in current_topics if topic in state.delegated_fields
            ]
            if delegated_repeat:
                return {
                    "quality": "delegated_topic_repeated",
                    "topics": delegated_repeat,
                    "instruction": (
                        "Игрок уже передал этот выбор мастеру. Выбери безопасный вариант "
                        "сам, сохрани его и не спрашивай снова. Если старт уже возможен, "
                        "вызови finalize_session_zero."
                    ),
                }

            repeated_topics = sorted(set(current_topics) & set(previous_topics))
            if repeated_topics:
                return {
                    "quality": "repeated_question_topic",
                    "topics": repeated_topics,
                    "instruction": (
                        "Формулировка изменилась, но тема вопроса та же. Не повторяй "
                        "её; используй последний ответ, сделай разумный вывод и подумай, "
                        "не пора ли уже начинать игру."
                    ),
                }

        if (
            len(current_topics) == 1
            and current_topics[0] in self.NARROW_CHARACTER_TOPICS
            and self._recent_narrow_question_count(state) >= 2
        ):
            return {
                "quality": "questionnaire_pattern",
                "topic": current_topics[0],
                "instruction": (
                    "Получилась серия узких анкетных вопросов. Не задавай ещё один "
                    "пункт карточки. Синтезируй безопасные детали из концепта и "
                    "переходи к стартовой сцене; если технический минимум можно "
                    "заполнить, вызови finalize_session_zero."
                ),
            }
        return None

    @classmethod
    def _effective_question_topics(
        cls,
        decision: SessionZeroInterviewModelDecision,
    ) -> list[str]:
        topics = [
            topic for topic in decision.question_topics if cls._valid_topic(topic)
        ]
        for topic in cls._infer_question_topics(decision.assistant_message):
            if topic not in topics:
                topics.append(topic)
        return topics

    @classmethod
    def _infer_question_topics(cls, message: str) -> list[str]:
        folded = cls._normalize_text(message)
        if not folded:
            return []
        topics: list[str] = []
        for topic, patterns in cls.TOPIC_PATTERNS:
            if any(pattern in folded for pattern in patterns):
                topics.append(topic)
        return topics

    @classmethod
    def _recent_narrow_question_count(
        cls,
        state: SessionZeroInterviewState,
    ) -> int:
        count = 0
        for item in reversed(state.messages):
            if item.get("role") != "assistant":
                continue
            topics = cls._infer_question_topics(item.get("content", ""))
            if len(topics) == 1 and topics[0] in cls.NARROW_CHARACTER_TOPICS:
                count += 1
                continue
            break
        return count

    @classmethod
    def _topic_has_value(
        cls,
        draft: SessionZeroInterviewDraft,
        topic: str,
    ) -> bool:
        if topic in {"world.boundaries", "world.boundaries_confirmed"}:
            return draft.world.boundaries_confirmed
        if not cls._valid_topic(topic):
            return False
        section_name, field_name = topic.split(".", 1)
        section = getattr(draft, section_name)
        return cls._has_value(getattr(section, field_name, None))

    @staticmethod
    def _valid_topic(topic: str) -> bool:
        if "." not in topic:
            return False
        section, field = topic.split(".", 1)
        allowed = {
            "world": {
                "setting_name",
                "genre",
                "premise",
                "tone",
                "themes",
                "boundaries",
                "boundaries_confirmed",
                "rules_system",
                "world_summary",
                "play_style",
                "narrative_style",
                "content_rating",
                "starting_location_name",
                "starting_situation",
                "starting_scene_title",
            },
            "character": {
                "name",
                "description",
                "appearance",
                "personality",
                "values",
                "fears",
                "desires",
                "voice",
                "speech_patterns",
                "biography",
                "capabilities",
                "limitations",
                "first_goal",
            },
        }
        return field in allowed.get(section, set())

    @classmethod
    def _is_delegation(cls, value: str) -> bool:
        folded = cls._normalize_text(value)
        return any(marker in folded for marker in cls.DELEGATION_MARKERS)

    @classmethod
    def _start_requested(cls, value: str) -> bool:
        folded = cls._normalize_text(value)
        return any(marker in folded for marker in cls.START_REQUEST_MARKERS)

    @classmethod
    def _assistant_claims_start(cls, value: str) -> bool:
        folded = cls._normalize_text(value)
        return any(marker in folded for marker in cls.START_CLAIM_MARKERS)

    @classmethod
    def _is_substantive_answer(cls, value: str) -> bool:
        clean = cls._text(value)
        return len(clean) >= 3 and not cls._is_delegation(clean)

    @classmethod
    def _terminal_start_message(cls, value: str) -> str:
        """Ready Session Zero must not play the opening or ask another question."""
        clean = cls._text(value)
        if (
            clean
            and "?" not in clean
            and not cls._assistant_claims_start(clean)
            and len(clean) <= 280
        ):
            return clean
        return "Основа готова. Начинаем с первой сцены."

    @classmethod
    def _safe_assistant_message(
        cls,
        value: str,
        *,
        ready: bool,
        quality_failed: bool,
    ) -> str:
        clean = cls._text(value)
        if ready:
            return cls._terminal_start_message(clean)
        if clean and not quality_failed:
            return clean
        return cls.SAFE_FALLBACK_MESSAGE

    @staticmethod
    def _combine_feedback(
        quality_feedback: dict | None,
        finalize_feedback: dict,
    ) -> dict:
        if quality_feedback is None:
            return finalize_feedback
        return {
            "issues": [quality_feedback, finalize_feedback],
            "instruction": (
                "Исправь обе проблемы за один ход: сохрани полезные данные, сам "
                "дострой технические пробелы и, если старт возможен, заверши нулевую "
                "сессию вместо нового вопроса."
            ),
        }


__all__ = [
    "RoleModelRouter",
    "SessionZeroAgent",
    "SessionZeroInterviewIncompleteError",
    "SessionZeroInterviewService",
    "asyncio",
]
