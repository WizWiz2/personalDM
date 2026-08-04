from __future__ import annotations

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


class ConversationalSessionZeroService:
    """Persist and materialize a friendly CLI session-zero conversation.

    The interview stores the player's own free-form answers after every question.
    It deliberately does not require an LLM: onboarding must remain usable before
    a provider is configured or while a cloud provider is rate-limited.
    """

    ANSWERS_KEY = "conversational_session_zero"
    QUESTIONS = (
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

    def __init__(self, session: AsyncSession):
        self._session = session
        self._campaigns = CampaignRepository(session)
        self._entities = EntityRepository(session)
        self._goals = GoalRepository(session)
        self._locations = LocationRepository(session)
        self._session_zero = SessionZeroService(session)

    async def get_answers(self, campaign_id: UUID) -> dict[str, str]:
        setup = await self._session_zero.get(campaign_id)
        raw = setup.custom_fields.get(self.ANSWERS_KEY, {})
        if not isinstance(raw, dict):
            return {}
        return {
            str(key): self._text(value)
            for key, value in raw.items()
            if self._text(value)
        }

    async def save_answer(
        self,
        campaign_id: UUID,
        key: str,
        value: str,
    ) -> dict[str, str]:
        if key not in {question.key for question in self.QUESTIONS}:
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
        return answers

    async def missing_questions(self, campaign_id: UUID) -> list[InterviewQuestion]:
        answers = await self.get_answers(campaign_id)
        return [question for question in self.QUESTIONS if question.key not in answers]

    async def finalize(self, campaign_id: UUID):
        answers = await self.get_answers(campaign_id)
        missing = [
            question.key
            for question in self.QUESTIONS
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
                play_style=answers["play_style"],
                content_rating="18+",
                player_character_id=hero.id,
                narrative_style=(
                    f"{answers['tone']}. В центре: {answers['play_style']}. "
                    "Не принимать решения и не описывать чувства за героя игрока."
                ),
                custom_fields={
                    self.ANSWERS_KEY: answers,
                    "wanted_content": answers["wanted"],
                    "interview_version": 1,
                },
            ),
        )
        completed = await self._session_zero.complete(campaign_id)
        await self._session.commit()
        return completed

    @staticmethod
    def summary(answers: dict[str, str]) -> str:
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
        return "\n".join(lines)

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
