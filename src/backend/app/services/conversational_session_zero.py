from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.goal_repo import GoalRepository
from app.db.repositories.location_repo import LocationRepository
from app.models.character import CharacterCreate
from app.models.goal import GoalCreate
from app.models.location import LocationCreate
from app.models.session_zero import SessionZeroUpdate
from app.services.session_zero_service import SessionZeroService


@dataclass(frozen=True)
class InterviewQuestion:
    key: str
    prompt: str
    hint: str
    allow_empty: bool = False
    source_keys: tuple[str, ...] = ()
    trigger_terms: tuple[str, ...] = ()


class _QuestionSequence(Sequence[InterviewQuestion]):
    """Compatibility sequence that can grow while the CLI interview runs."""

    def __init__(self, service: ConversationalSessionZeroService):
        self._service = service

    def __iter__(self) -> Iterator[InterviewQuestion]:
        yielded: set[str] = set()
        while True:
            current = self._service.questions_for_answers(
                self._service._answers_cache
            )
            question = next(
                (item for item in current if item.key not in yielded),
                None,
            )
            if question is None:
                return
            yielded.add(question.key)
            yield question

    def __len__(self) -> int:
        return len(
            self._service.questions_for_answers(self._service._answers_cache)
        )

    def __getitem__(self, index):
        return self._service.questions_for_answers(
            self._service._answers_cache
        )[index]


class ConversationalSessionZeroService:
    """Persist and materialize a friendly adaptive session-zero conversation.

    The interview stores the player's own free-form answers after every question.
    A deterministic branching layer asks only relevant follow-ups, while the core
    onboarding remains usable before a provider is configured or during rate limits.
    """

    ANSWERS_KEY = "conversational_session_zero"
    CORE_QUESTIONS = (
        InterviewQuestion(
            "world",
            "Во что хочется играть? Опиши мир, жанр, эпоху и уровень магии/технологий.",
            "Например: мрачное городское фэнтези без спасения мира.",
        ),
        InterviewQuestion(
            "adventure",
            "Какого приключения хочется именно сейчас?",
            "Можно назвать завязку, масштаб и то, чем хочется заниматься.",
        ),
        InterviewQuestion(
            "play_style",
            "Что должно быть в центре игры?",
            "Диалоги, исследование, интриги, управление, бои — в любой смеси.",
        ),
        InterviewQuestion(
            "tone",
            "Какой тон и темп тебе приятны?",
            "Например: серьёзно, атмосферно, без постоянной угрозы и спешки.",
        ),
        InterviewQuestion(
            "wanted",
            "Что особенно хочется увидеть в этой кампании?",
            "Темы, отношения, типы сцен или конкретные игровые удовольствия.",
        ),
        InterviewQuestion(
            "boundaries",
            "Чего в игре точно не должно быть?",
            "Напиши «нет», если дополнительных ограничений нет.",
            allow_empty=True,
        ),
        InterviewQuestion(
            "hero_name",
            "Как зовут твоего героя?",
            "Можно указать временное имя и поменять его позже.",
        ),
        InterviewQuestion(
            "hero_concept",
            "Кто твой герой: прошлое, характер и место в мире?",
            "Обычного абзаца достаточно.",
        ),
        InterviewQuestion(
            "hero_capabilities",
            "Что герой умеет особенно хорошо?",
            "Перечисли сильные стороны через запятую.",
        ),
        InterviewQuestion(
            "hero_limitations",
            "В чём герой ограничен или уязвим?",
            "Недостатки, запреты, нехватка знаний или ресурсов.",
        ),
        InterviewQuestion(
            "hero_goal",
            "Чего герой хочет добиться в начале кампании?",
            "Это станет его первой активной целью.",
        ),
        InterviewQuestion(
            "hero_values",
            "Что для героя важно и чего он боится потерять?",
            "Ответ станет основой ценностей и страхов, без додумывания за тебя.",
        ),
        InterviewQuestion(
            "hero_appearance",
            "Как герой выглядит?",
            "Достаточно нескольких узнаваемых деталей.",
        ),
        InterviewQuestion(
            "hero_voice",
            "Как герой обычно говорит?",
            "Тембр, манера речи, любимая лексика или краткость.",
        ),
        InterviewQuestion(
            "opening_location",
            "Где должна начаться первая сцена?",
            "Конкретное место: дом, корабль, тракт, дворец, станция и т.п.",
        ),
        InterviewQuestion(
            "opening_situation",
            "Что происходит в первый момент игры?",
            "Задай ситуацию, но оставь решение и первую реплику за героем.",
        ),
    )
    ADAPTIVE_QUESTIONS = (
        InterviewQuestion(
            "combat_style",
            "Ты упомянул бои. Какими они должны быть: редкими или частыми, тактическими или быстрыми, насколько опасными?",
            "Можно отдельно указать желаемую летальность и отношение к случайной смерти.",
            source_keys=("adventure", "play_style", "wanted"),
            trigger_terms=(
                "бой",
                "бои",
                "боев",
                "боёв",
                "сраж",
                "тактик",
                "тактич",
                "combat",
            ),
        ),
        InterviewQuestion(
            "relationship_style",
            "Ты упомянул романтику или близкие отношения. Какой темп, инициативу NPC и уровень откровенности ты хочешь?",
            "Например: медленное развитие, NPC могут проявлять инициативу, интимные сцены остаются за кадром.",
            source_keys=("play_style", "wanted", "adventure"),
            trigger_terms=(
                "роман",
                "любов",
                "отношен",
                "эрот",
                "гарем",
                "romance",
            ),
        ),
        InterviewQuestion(
            "horror_safety",
            "Ты выбрал хоррор или страшные темы. Что должно пугать, а где проходит жёсткая граница?",
            "Можно разделить психологический ужас, телесный хоррор, беспомощность и скримеры.",
            source_keys=("world", "tone", "wanted", "adventure"),
            trigger_terms=("хорр", "ужас", "страш", "кошмар", "horror"),
        ),
        InterviewQuestion(
            "management_style",
            "Ты упомянул политику, власть или управление. Каким масштабом герой должен реально распоряжаться?",
            "Например: маленькая организация, город, королевство; лично или через советников.",
            source_keys=("adventure", "play_style", "wanted"),
            trigger_terms=(
                "полит",
                "интриг",
                "власть",
                "организац",
                "королев",
                "управлять",
                "management",
            ),
        ),
        InterviewQuestion(
            "sandbox_style",
            "Ты описал свободную игру или песочницу. Насколько активно мир должен сам подбрасывать возможности и последствия?",
            "От полностью player-driven игры до активного мира с несколькими параллельными линиями.",
            source_keys=("adventure", "play_style"),
            trigger_terms=("песоч", "sandbox", "открытый мир", "свободная игра"),
        ),
        InterviewQuestion(
            "canon_fidelity",
            "Похоже, это известный сеттинг. Насколько строго держаться официального канона и можно ли его менять решениями героя?",
            "Можно выбрать строгий канон, мягкую адаптацию или альтернативную версию мира.",
            source_keys=("world",),
            trigger_terms=("канон", "вселенн", "по мотивам", "setting of"),
        ),
    )
    FINAL_QUESTION = InterviewQuestion(
        "additional_context",
        "Есть ли о тебе как об игроке или об этой кампании что-то важное, чего я ещё не спросил?",
        "Можно написать «нет» или добавить любую привычку, пожелание либо особенность игры.",
        allow_empty=True,
    )
    ALL_QUESTIONS = (*CORE_QUESTIONS, *ADAPTIVE_QUESTIONS, FINAL_QUESTION)

    def __init__(self, session: AsyncSession):
        self._session = session
        self._campaigns = CampaignRepository(session)
        self._entities = EntityRepository(session)
        self._goals = GoalRepository(session)
        self._locations = LocationRepository(session)
        self._session_zero = SessionZeroService(session)
        self._answers_cache: dict[str, str] = {}
        # Legacy CLI reads this public attribute; the sequence recomputes branches
        # after every saved answer instead of freezing a questionnaire up front.
        self.QUESTIONS = _QuestionSequence(self)

    async def get_answers(self, campaign_id: UUID) -> dict[str, str]:
        setup = await self._session_zero.get(campaign_id)
        raw = setup.custom_fields.get(self.ANSWERS_KEY, {})
        if not isinstance(raw, dict):
            self._answers_cache = {}
            return {}
        answers = {
            str(key): self._text(value)
            for key, value in raw.items()
            if self._text(value)
        }
        self._answers_cache = answers
        return answers

    async def save_answer(
        self,
        campaign_id: UUID,
        key: str,
        value: str,
    ) -> dict[str, str]:
        if key not in {question.key for question in self.ALL_QUESTIONS}:
            raise ValueError(f"Unknown session-zero interview key: {key}")
        setup = await self._session_zero.get(campaign_id)
        custom = dict(setup.custom_fields or {})
        answers = dict(custom.get(self.ANSWERS_KEY) or {})
        clean = self._text(value)
        if clean:
            answers[key] = clean
        else:
            answers.pop(key, None)
        custom[self.ANSWERS_KEY] = answers
        await self._session_zero.update(
            campaign_id,
            SessionZeroUpdate(custom_fields=custom),
        )
        await self._session.commit()
        self._answers_cache = {
            str(answer_key): self._text(answer_value)
            for answer_key, answer_value in answers.items()
            if self._text(answer_value)
        }
        return dict(self._answers_cache)

    def questions_for_answers(
        self,
        answers: dict[str, str],
    ) -> list[InterviewQuestion]:
        questions = list(self.CORE_QUESTIONS)
        for question in self.ADAPTIVE_QUESTIONS:
            source = " ".join(
                answers.get(key, "") for key in question.source_keys
            ).casefold()
            if any(term.casefold() in source for term in question.trigger_terms):
                questions.append(question)
        questions.append(self.FINAL_QUESTION)
        return questions

    async def missing_questions(self, campaign_id: UUID) -> list[InterviewQuestion]:
        answers = await self.get_answers(campaign_id)
        return [
            question
            for question in self.questions_for_answers(answers)
            if question.key not in answers
        ]

    async def finalize(self, campaign_id: UUID):
        answers = await self.get_answers(campaign_id)
        active_questions = self.questions_for_answers(answers)
        missing = [
            question.key
            for question in active_questions
            if question.key not in answers and not question.allow_empty
        ]
        if missing:
            raise ValueError("Interview is incomplete: " + ", ".join(missing))

        campaign = await self._campaigns.get_by_id(campaign_id)
        if not campaign:
            raise ValueError("Campaign not found")

        location = await self._locations.create(
            campaign_id,
            LocationCreate(
                canonical_name=answers["opening_location"],
                description=answers["opening_situation"],
                atmosphere=answers["tone"],
                custom_fields={"source": "conversational_session_zero"},
            ),
        )
        values_and_fears = self._items(answers["hero_values"])
        capabilities = self._items(answers["hero_capabilities"])
        limitations = self._items(answers["hero_limitations"])
        hero = await self._entities.create_character(
            campaign_id,
            CharacterCreate(
                canonical_name=answers["hero_name"],
                description=answers["hero_concept"],
                appearance=answers["hero_appearance"],
                personality=answers["hero_concept"],
                values=values_and_fears,
                fears=values_and_fears,
                desires=[answers["hero_goal"]],
                voice=answers["hero_voice"],
                speech_patterns=answers["hero_voice"],
                biography=answers["hero_concept"],
                emotional_state="определяет игрок в ходе сцены",
                current_location_id=location.id,
                current_intentions=[answers["hero_goal"]],
                custom_fields={
                    "capabilities": capabilities,
                    "limitations": limitations,
                    "source": "conversational_session_zero",
                },
            ),
        )
        await self._goals.create(
            hero.id,
            GoalCreate(description=answers["hero_goal"], priority=100),
        )

        boundaries = self._items(answers.get("boundaries", ""))
        if len(boundaries) == 1 and boundaries[0].casefold() in {
            "нет",
            "никаких",
            "none",
            "no",
        }:
            boundaries = []
        themes = self._items(answers["wanted"])
        adaptive = self._adaptive_preferences(answers)
        adaptive_text = "; ".join(adaptive.values())
        play_style = answers["play_style"]
        if adaptive_text:
            play_style += f". Уточнения по ответам игрока: {adaptive_text}"
        await self._session_zero.update(
            campaign_id,
            SessionZeroUpdate(
                setting_name=self._setting_name(answers["world"], campaign.name),
                genre=answers["world"],
                premise=answers["adventure"],
                tone=answers["tone"],
                themes=themes,
                boundaries=boundaries,
                boundaries_confirmed=True,
                rules_system="свободная повествовательная система",
                world_summary=answers["world"],
                starting_situation=answers["opening_situation"],
                starting_location_id=location.id,
                starting_scene_title=f"Начало: {answers['opening_location']}",
                play_style=play_style,
                content_rating="18+",
                player_character_id=hero.id,
                narrative_style=(
                    f"{answers['tone']}. В центре: {play_style}. "
                    "Не принимать решения и не описывать чувства за героя игрока."
                ),
                custom_fields={
                    self.ANSWERS_KEY: answers,
                    "wanted_content": answers["wanted"],
                    "adaptive_preferences": adaptive,
                    "interview_version": 2,
                },
            ),
        )
        completed = await self._session_zero.complete(campaign_id)
        await self._session.commit()
        return completed

    @classmethod
    def summary(cls, answers: dict[str, str]) -> str:
        lines = [
            f"Мир и жанр: {answers.get('world', '—')}",
            f"Приключение: {answers.get('adventure', '—')}",
            f"Стиль: {answers.get('play_style', '—')}",
            f"Тон: {answers.get('tone', '—')}",
            f"Хочется: {answers.get('wanted', '—')}",
            f"Границы: {answers.get('boundaries', 'нет дополнительных') or 'нет дополнительных'}",
            f"Герой: {answers.get('hero_name', '—')} — {answers.get('hero_concept', '—')}",
            f"Сильные стороны: {answers.get('hero_capabilities', '—')}",
            f"Ограничения: {answers.get('hero_limitations', '—')}",
            f"Первая цель: {answers.get('hero_goal', '—')}",
            f"Старт: {answers.get('opening_location', '—')} — {answers.get('opening_situation', '—')}",
        ]
        labels = {question.key: question.prompt for question in cls.ADAPTIVE_QUESTIONS}
        for key, value in cls._adaptive_preferences(answers).items():
            lines.append(f"Уточнение — {labels.get(key, key)}: {value}")
        extra = answers.get("additional_context")
        if extra and extra.casefold() not in {"нет", "none", "no"}:
            lines.append(f"Дополнительно: {extra}")
        return "\n".join(lines)

    @classmethod
    def _adaptive_preferences(cls, answers: dict[str, str]) -> dict[str, str]:
        return {
            question.key: answers[question.key]
            for question in cls.ADAPTIVE_QUESTIONS
            if question.key in answers
        }

    @staticmethod
    def _setting_name(world: str, fallback: str) -> str:
        first = world.split(".", 1)[0].strip()
        return (first or fallback)[:200]

    @classmethod
    def _items(cls, value: str) -> list[str]:
        normalized = value.replace(";", ",").replace("\n", ",")
        result: list[str] = []
        for item in normalized.split(","):
            clean = cls._text(item)
            if clean and clean not in result:
                result.append(clean)
        return result or ([cls._text(value)] if cls._text(value) else [])

    @staticmethod
    def _text(value: object) -> str:
        return " ".join(str(value or "").split())
