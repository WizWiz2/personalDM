from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections import Counter, deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.memory import resolve_proposal
from app.api.world_state import CharacterDraft, create_character_from_draft
from app.db.engine import Base
from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.event_repo import EventRepository
from app.db.repositories.fact_repo import FactRepository
from app.db.repositories.proposed_change_repo import ProposedChangeRepository
from app.db.repositories.provider_config_repo import ProviderConfigRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.repositories.turn_repo import TurnRepository
from app.db.tables import (
    Belief,
    CharacterGoal,
    Entity,
    Event,
    ProposedChange,
    RelationshipAssertion,
    Scene,
    SceneThesis,
    Turn as DBTurn,
)
from app.models.campaign import CampaignCreate, CampaignUpdate
from app.models.character import CharacterUpdate
from app.models.entity import EntityCreate, EntityType
from app.models.event import EventCreate
from app.models.proposed_change import ProposalAction
from app.models.provider_config import ProviderConfigCreate
from app.models.scene import SceneCreate, SceneUpdate
from app.models.scene_thesis import SceneThesisCreate, SceneThesisUpdate, ThesisType
from app.models.turn import ChatMessage, TurnCreate
from app.providers.llm_provider import LLMProvider, LLMProviderError
from app.services.campaign_service import CampaignService
from app.services.context_compiler import ContextCompiler
from app.services.role_model_router import ModelRole, RoleModelRouter
from app.services.thesis_curator import ThesisCurator
from app.services.turn_runner import TurnRunner

try:
    from .simulation_scenario import NPCS, PHASES, NpcConcept, ScenarioPhase
except ImportError:
    from simulation_scenario import NPCS, PHASES, NpcConcept, ScenarioPhase


OUTCOME_PATTERNS = (
    r"\bя (?:успешно|точно|немедленно)\b",
    r"\bя (?:нахожу|обнаруживаю|открываю|побеждаю|убиваю|исцеляю|решаю)\b",
    r"\b(?:дверь|ворота|замок|враг|ритуал) (?:открывается|ломается|побеждён|срабатывает)\b",
    r"\bI (?:successfully|discover|find|open|defeat|solve)\b",
)
WORD_PATTERN = re.compile(r"[\w]+", flags=re.UNICODE)
JSON_OBJECT_PATTERN = re.compile(r"\{.*\}", flags=re.DOTALL)


class ObjectiveEvaluation(BaseModel):
    status: Literal["progressing", "resolved", "failed", "blocked"] = "progressing"
    evidence: str = ""
    outcome_summary: str | None = None
    confirmed_pulses: list[int] = Field(default_factory=list)


@dataclass(frozen=True)
class PlayerDecision:
    target: str
    mode: Literal["action", "dialogue", "question", "plan", "decision"]
    intent: str

    def render(self) -> str:
        return f"[/talk {self.target}] {self.intent.strip()}"


@dataclass
class PhaseRuntime:
    index: int
    phase: ScenarioPhase
    scene_id: UUID
    location_id: UUID
    active_characters: dict[str, UUID]
    phase_turn: int = 0
    injected_pulses: set[int] = field(default_factory=set)
    confirmed_pulses: set[int] = field(default_factory=set)


@dataclass
class SimulationState:
    run_id: str
    campaign_id: str | None = None
    logical_turn: int = 1
    phase_index: int = 0
    phase_turn: int = 0
    injected_pulses: list[int] = field(default_factory=list)
    confirmed_pulses: list[int] = field(default_factory=list)
    consecutive_failures: int = 0
    completed: bool = False

    @classmethod
    def load(cls, path: Path) -> "SimulationState | None":
        if not path.exists():
            return None
        try:
            return cls(**json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            return None

    def save(self, path: Path) -> None:
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)


class TraceStore:
    def __init__(self, path: Path):
        self.path = path
        self.records: dict[int, dict] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    record = json.loads(line)
                    self.records[int(record["turn"])] = record
                except Exception:
                    continue

    def upsert(self, record: dict) -> None:
        self.records[int(record["turn"])] = record
        self.flush()

    def flush(self) -> None:
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        with temp.open("w", encoding="utf-8") as handle:
            for turn in sorted(self.records):
                handle.write(json.dumps(self.records[turn], ensure_ascii=False) + "\n")
        temp.replace(self.path)

    def write_play_log(self, path: Path, total_turns: int) -> None:
        temp = path.with_suffix(path.suffix + ".tmp")
        with temp.open("w", encoding="utf-8") as handle:
            handle.write("REALISTIC AUTONOMOUS CAMPAIGN V2\n\n")
            for number in sorted(self.records):
                record = self.records[number]
                handle.write("=" * 72 + "\n")
                handle.write(
                    f"TURN {number}/{total_turns} | {record.get('phase_title', record.get('phase'))}\n"
                )
                handle.write(f"OBJECTIVE: {record.get('objective', '')}\n")
                handle.write(
                    "ACTIVE NPCS: " + ", ".join(record.get("active_npcs", [])) + "\n"
                )
                player = record.get("player", {})
                handle.write(
                    f"PLAYER [{player.get('mode')} -> {player.get('target')}]: "
                    f"{player.get('intent', '')}\n"
                )
                handle.write("-" * 72 + "\n")
                handle.write(f"DM: {record.get('dm', '')}\n")
                evaluation = record.get("evaluation") or {}
                if evaluation:
                    handle.write(
                        f"OBJECTIVE STATUS: {evaluation.get('status')} | "
                        f"{evaluation.get('evidence', '')}\n"
                    )
                for item in record.get("accepted", []):
                    handle.write(f"ACCEPTED: {item}\n")
                for item in record.get("rejected", []):
                    handle.write(f"REJECTED: {item}\n")
                handle.write("THESES:\n")
                for item in record.get("active_theses", []):
                    handle.write(f" - [{item.get('type')}] {item.get('text')}\n")
                handle.write("\n")
        temp.replace(path)


class PlayerPolicy:
    MODES = ("question", "action", "dialogue", "plan", "action", "decision")

    def __init__(self) -> None:
        self.recent_fingerprints: deque[str] = deque(maxlen=16)
        self.mode_counts: Counter[str] = Counter()
        self.target_counts: Counter[str] = Counter()
        self.rejected_outcomes = 0
        self.repeated_actions = 0
        self.fallbacks = 0

    @staticmethod
    def fingerprint(value: str) -> str:
        tokens = [token.casefold() for token in WORD_PATTERN.findall(value)]
        return " ".join(tokens[:60])

    def preferred_mode(self, turn_number: int) -> str:
        return self.MODES[(turn_number - 1) % len(self.MODES)]

    def suggested_target(self, active_npcs: list[str], mode: str) -> str:
        if mode in {"question", "dialogue", "plan", "decision"} and active_npcs:
            return min(
                active_npcs,
                key=lambda name: (
                    self.target_counts[name.casefold()],
                    name.casefold(),
                ),
            )
        return "narrator"

    def validate(
        self,
        decision: PlayerDecision,
        active_npcs: list[str],
    ) -> tuple[bool, str | None]:
        active = {name.casefold() for name in active_npcs}
        if decision.target.casefold() != "narrator" and decision.target.casefold() not in active:
            return False, f"target {decision.target!r} is not active"
        if not decision.intent.strip() or len(decision.intent) > 700:
            return False, "intent is empty or too long"
        if any(re.search(pattern, decision.intent, re.IGNORECASE) for pattern in OUTCOME_PATTERNS):
            self.rejected_outcomes += 1
            return False, "intent declares an outcome"
        fingerprint = self.fingerprint(decision.intent)
        if fingerprint and fingerprint in self.recent_fingerprints:
            self.repeated_actions += 1
            return False, "intent repeats a recent action"
        return True, None

    def remember(self, decision: PlayerDecision) -> None:
        self.recent_fingerprints.append(self.fingerprint(decision.intent))
        self.mode_counts[decision.mode] += 1
        self.target_counts[decision.target.casefold()] += 1

    def fallback(
        self,
        active_npcs: list[str],
        mode: str,
        objective: str,
        latest_result: str,
        active_theses: list[str],
        turn_number: int,
        *,
        count_fallback: bool = True,
    ) -> PlayerDecision:
        if count_fallback:
            self.fallbacks += 1
        target = self.suggested_target(active_npcs, mode)
        hook = next((value for value in reversed(active_theses) if value.strip()), objective)
        hook = " ".join(hook.split())[:180]
        consequence = " ".join(latest_result.split())[-180:] if latest_result else ""
        variants = [
            (
                "question",
                target,
                f"Я спрашиваю {target}, какой наблюдаемый признак подтвердит или опровергнет: «{hook}». Мне нужен конкретный ответ, который изменит наш следующий шаг.",
            ),
            (
                "dialogue",
                target,
                f"Я кратко объясняю {target}, что после последнего события нам нужно решить задачу «{objective}», и прошу назвать личный риск, о котором группа ещё не договорилась.",
            ),
            (
                "plan",
                target,
                f"Я предлагаю проверить один факт из текущей ситуации: «{hook}». Сначала наблюдение, затем решение, без попытки заранее объявить результат.",
            ),
            (
                "decision",
                target,
                f"Я формулирую два допустимых варианта для цели «{objective}» и прошу {target} указать, какой из них меньше противоречит увиденному нами последствию: «{consequence or hook}».",
            ),
            (
                "action",
                "narrator",
                f"Я использую обычные навыки руиниста, чтобы проверить конкретную деталь, связанную с тезисом «{hook}»: осматриваю следы, крепления и доступные пути, не касаясь магических элементов и не объявляя успех.",
            ),
            (
                "action",
                "narrator",
                f"Я сверяю последнее наблюдение «{consequence or hook}» со своей верёвкой, фонарём и набором отмычек, чтобы понять, какой безопасный следующий тест возможен в рамках цели «{objective}».",
            ),
        ]
        offset = turn_number % len(variants)
        for index in range(len(variants)):
            candidate_mode, candidate_target, intent = variants[(offset + index) % len(variants)]
            if candidate_target == "narrator" or candidate_target in active_npcs:
                decision = PlayerDecision(
                    target=candidate_target,
                    mode=candidate_mode,
                    intent=intent,
                )
                valid, _ = self.validate(decision, active_npcs)
                if valid:
                    return decision
        return PlayerDecision(
            target="narrator",
            mode="action",
            intent=(
                f"Я останавливаюсь и вслух фиксирую, что именно изменилось после хода {turn_number}; "
                f"затем выбираю новую проверку, напрямую связанную с целью «{objective}».")
        )


def parse_json_object(raw: str) -> dict:
    clean = raw.strip()
    if clean.startswith("```"):
        lines = clean.splitlines()
        clean = "\n".join(lines[1:-1]).strip()
    try:
        value = json.loads(clean)
        if isinstance(value, dict):
            return value
    except Exception:
        pass
    match = JSON_OBJECT_PATTERN.search(clean)
    if not match:
        raise ValueError("response does not contain a JSON object")
    value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("response JSON is not an object")
    return value


def parse_player_decision(raw: str, active_npcs: list[str]) -> PlayerDecision:
    data = parse_json_object(raw)
    canonical = {name.casefold(): name for name in active_npcs}
    target_raw = str(data.get("target", "narrator")).strip()
    target = canonical.get(
        target_raw.casefold(),
        "narrator" if target_raw.casefold() == "narrator" else target_raw,
    )
    mode = str(data.get("mode", "action")).strip().casefold()
    if mode not in {"action", "dialogue", "question", "plan", "decision"}:
        mode = "action"
    return PlayerDecision(
        target=target,
        mode=mode,
        intent=str(data.get("intent", "")).strip(),
    )


def eldon_card() -> CharacterDraft:
    return CharacterDraft(
        canonical_name="Eldon",
        description="Практичный человек-руинист, который выживает благодаря осторожности и сотрудничеству.",
        appearance="Потёртый дорожный плащ, короткие тёмные волосы и шрам над правой бровью.",
        personality="Практичный, подозрительный к лёгким ответам, суховатый, но умеющий слушать.",
        values=["жизнь спутников", "проверяемые свидетельства", "свобода выбора"],
        fears=["стать чужим инструментом", "потерять людей из-за безрассудного любопытства"],
        desires=["понять цитадель", "уйти с реликтовым ключом и живой группой"],
        voice="Низкий, прямой, с сухим юмором.",
        speech_patterns="Задаёт конкретные вопросы и называет риск до действия.",
        biography="Бывший охранник караванов и исследователь руин, знакомый с обычными ловушками.",
        backstory_public="Исчезнувший покровитель выбрал его запасным исследователем.",
        secrets=["Элдон подозревает, что покровитель изначально считал его расходным материалом."],
        emotional_state="настороженное любопытство",
        current_intentions=["понять, кому можно доверять", "найти реликтовый ключ"],
        goals=["Найти реликтовый ключ", "Сохранить экспедицию", "Понять выбор покровителя"],
        capabilities=["осматривать обычные механизмы", "работать простыми отмычками", "сражаться кинжалом", "лазать с верёвкой", "замечать практическую опасность"],
        limitations=["не умеет колдовать", "не распознаёт сверхъестественное без помощи", "не использует продвинутую технику", "не объявляет результаты своих действий"],
        equipment=["дорожный фонарь", "пеньковая верёвка", "обычный кинжал", "набор простых отмычек", "фляга"],
        initial_beliefs=["Каждый источник о реликтовом ключе неполон."],
        visual_profile={"palette": "brown, iron and weathered green"},
    )


def deterministic_fallback_card(seed: NpcConcept, location_id: UUID) -> CharacterDraft:
    role = seed.campaign_role
    tone = seed.tone
    concept_words = " ".join(seed.concept.split())
    return CharacterDraft(
        canonical_name=seed.name,
        description=f"{role.capitalize()}; в экспедиции выполняет одну чёткую функцию и скрывает личную ставку.",
        appearance=f"Походная одежда {seed.name}, приспособленная к роли: {role}; заметная деталь связана с профессией.",
        face_description=f"Выражение лица {seed.name} отражает манеру: {tone}.",
        body_description="Телосложение и осанка соответствуют повседневной работе, без сверхъестественных особенностей.",
        immutable_features=f"Узнаваемая профессиональная деталь {seed.name}, которую нельзя случайно потерять между сценами.",
        personality=tone,
        values=["профессиональная компетентность", "личная автономия", "выживание группы"],
        fears=[f"что станет известно: {concept_words[:180]}", "потерять контроль над своей ролью в экспедиции"],
        desires=["выполнить свою задачу", "сохранить личный секрет до подходящего момента"],
        voice=f"Манера речи следует описанию «{tone}» и отличается от остальных участников.",
        speech_patterns=f"Использует лексику своей роли ({role}), отвечает на конкретный вопрос и не повторяет универсальные формулы о риске.",
        biography=f"Присоединился к экспедиции как {role} после события, связанного с личным секретом.",
        backstory_public=f"Группе известен как {role}.",
        secrets=[seed.concept],
        emotional_state=f"собранность, окрашенная чертой: {tone}",
        current_intentions=["проявить полезность в текущей сцене", "не раскрыть секрет без причины"],
        goals=["продвинуть текущую цель экспедиции", "разрешить личный конфликт, не разрушив группу"],
        capabilities=[f"надёжно применять знания по роли: {role}", "замечать детали, относящиеся к своей профессии"],
        limitations=["не использует способности вне своей роли", "не знает чужих секретов без передачи знания"],
        equipment=[f"личный дорожный набор {seed.name}", f"профессиональные инструменты {seed.name}"],
        initial_beliefs=["Цитадель опаснее, чем утверждают публичные источники."],
        visual_profile={"role": role, "tone": tone, "fallback": True},
        current_location_id=location_id,
    )


async def build_character_card(
    provider: LLMProvider,
    config,
    api_key: str | None,
    seed: NpcConcept,
    location_id: UUID,
) -> tuple[CharacterDraft, str]:
    prompt = f"""Создай различимую карточку NPC для долгой русскоязычной кампании.
Верни только JSON с ключами CharacterDraft: canonical_name, description, appearance,
face_description, body_description, immutable_features, personality, values, fears,
desires, voice, speech_patterns, biography, backstory_public, secrets,
emotional_state, current_intentions, goals, capabilities, limitations, equipment,
initial_beliefs, visual_profile.

Имя: {seed.name}
Концепция: {seed.concept}
Роль: {seed.campaign_role}
Тон: {seed.tone}

Требования:
- Все текстовые поля на русском языке.
- Каждая карточка должна иметь отличимый голос и профессиональную лексику.
- 1-4 элемента в каждом списке.
- Никаких неограниченных сил, техники или предметов вне роли.
- Equipment содержит уникальные конкретные экземпляры с именем владельца.
"""
    raw = ""
    try:
        async for token in provider.generate_stream(
            [ChatMessage(role="system", content=prompt)],
            config,
            api_key,
            max_tokens=1400,
            temperature=0.45,
        ):
            raw += token
        payload = parse_json_object(raw)
        payload["current_location_id"] = location_id
        card = CharacterDraft.model_validate(payload)
        return card, "model"
    except Exception as first_error:
        repair_prompt = f"""Исправь JSON карточки NPC {seed.name}.
Верни только валидный JSON для CharacterDraft, без markdown. Все поля на русском.
Ошибка проверки: {first_error}
Повреждённый ответ:
{raw[-6000:]}
"""
        repaired = ""
        try:
            async for token in provider.generate_stream(
                [ChatMessage(role="system", content=repair_prompt)],
                config,
                api_key,
                max_tokens=1400,
                temperature=0.1,
            ):
                repaired += token
            payload = parse_json_object(repaired)
            payload["current_location_id"] = location_id
            card = CharacterDraft.model_validate(payload)
            return card, "repair"
        except Exception:
            return deterministic_fallback_card(seed, location_id), "fallback"


class ScenarioDirector:
    def __init__(
        self,
        session: AsyncSession,
        campaign_id: UUID,
        player_id: UUID,
        provider: LLMProvider,
        config,
        api_key: str | None,
        stats: Counter,
    ):
        self.session = session
        self.campaign_id = campaign_id
        self.player_id = player_id
        self.provider = provider
        self.config = config
        self.api_key = api_key
        self.stats = stats
        self.campaign_service = CampaignService(session)
        self.entities = EntityRepository(session)
        self.scenes = SceneRepository(session)
        self.events = EventRepository(session)
        self.curator = ThesisCurator(session)
        self.characters: dict[str, UUID] = {"Eldon": player_id}
        self.current: PhaseRuntime | None = None

    async def restore_characters(self) -> None:
        for entity in await self.entities.list_by_campaign(self.campaign_id, "character"):
            self.characters[entity.canonical_name] = entity.id

    async def ensure_npc(self, name: str, location_id: UUID) -> UUID:
        if name in self.characters:
            return self.characters[name]
        seed = NPCS[name]
        card, source = await build_character_card(
            self.provider,
            self.config,
            self.api_key,
            seed,
            location_id,
        )
        self.stats[f"character_builder_{source}"] += 1
        built = await create_character_from_draft(
            self.campaign_id,
            card.model_copy(update={"current_location_id": location_id}),
            session=self.session,
        )
        self.characters[name] = built.character.id
        return built.character.id

    async def close_current(self, status: str, summary: str, source_turn_id: UUID | None) -> None:
        if not self.current:
            return
        await self.events.create(
            self.campaign_id,
            EventCreate(
                event_type="scene_outcome",
                description=f"[{status}] {summary}",
                location_id=self.current.location_id,
                importance="important",
                participant_ids=list(self.current.active_characters.values()),
                source_turns=[source_turn_id] if source_turn_id else [],
            ),
        )
        await self.curator.close_scene(self.current.scene_id)
        await self.scenes.update(
            self.current.scene_id,
            SceneUpdate(status="completed"),
        )
        await self.session.commit()

    async def enter_phase(self, index: int, state: SimulationState) -> PhaseRuntime:
        phase = PHASES[index]
        existing_scenes = await self.scenes.list_by_campaign(self.campaign_id)
        existing = next(
            (scene for scene in existing_scenes if scene.title == phase.title),
            None,
        )
        if existing and existing.status == "active":
            participants = await self.scenes.get_participants(existing.id)
            active = {
                name: entity_id
                for name, entity_id in self.characters.items()
                if entity_id in participants
            }
            player = await self.entities.get_character(self.player_id)
            runtime = PhaseRuntime(
                index=index,
                phase=phase,
                scene_id=existing.id,
                location_id=player.current_location_id,
                active_characters=active,
                phase_turn=state.phase_turn,
                injected_pulses=set(state.injected_pulses),
                confirmed_pulses=set(state.confirmed_pulses),
            )
            self.current = runtime
            return runtime

        location = await self.entities.create(
            self.campaign_id,
            EntityCreate(
                entity_type=EntityType.LOCATION,
                canonical_name=phase.title,
                description=phase.location_description,
                custom_fields={"scenario_phase": phase.slug},
            ),
        )
        for name in phase.introduced_npcs:
            await self.ensure_npc(name, location.id)
        active = {name: self.characters[name] for name in phase.active_npcs}
        active["Eldon"] = self.player_id
        scene = await self.scenes.create(
            self.campaign_id,
            SceneCreate(
                title=phase.title,
                location_description=phase.location_description,
                mood=phase.mood,
                tension=phase.tension,
            ),
        )
        for character_id in active.values():
            await self.scenes.add_participant(scene.id, character_id)
            await self.entities.update_character(
                character_id,
                CharacterUpdate(current_location_id=location.id),
            )
        await self.campaign_service.update_campaign(
            self.campaign_id,
            CampaignUpdate(current_scene_id=scene.id),
        )
        await self.scenes.create_thesis(
            scene.id,
            SceneThesisCreate(
                thesis_type=ThesisType.INTENTION,
                text=f"Режиссёрская граница: {phase.director_note}",
                priority=10,
                visibility="dm",
                pinned=True,
                related_entity_ids=[self.player_id],
            ),
        )
        for seed in phase.opening_theses:
            await self.scenes.create_thesis(
                scene.id,
                SceneThesisCreate(
                    thesis_type=seed.thesis_type,
                    text=seed.text,
                    priority=seed.priority,
                    visibility=seed.visibility,
                    related_entity_ids=[
                        self.characters[name]
                        for name in seed.related_names
                        if name in self.characters
                    ],
                ),
            )
        await self.events.create(
            self.campaign_id,
            EventCreate(
                event_type="scene_transition",
                description=f"Экспедиция вошла в сцену {phase.title}. Цель: {phase.objective}",
                location_id=location.id,
                importance="important",
                participant_ids=list(active.values()),
            ),
        )
        await self.session.commit()
        runtime = PhaseRuntime(
            index=index,
            phase=phase,
            scene_id=scene.id,
            location_id=location.id,
            active_characters=active,
        )
        self.current = runtime
        return runtime

    async def inject_due_pulses(self, runtime: PhaseRuntime, hard_limit: int) -> None:
        thresholds = [
            max(2, round(pulse.at_fraction * hard_limit))
            for pulse in runtime.phase.pulses
        ]
        for pulse_index, pulse in enumerate(runtime.phase.pulses):
            if pulse_index in runtime.injected_pulses:
                continue
            if runtime.phase_turn < thresholds[pulse_index]:
                continue
            await self.scenes.create_thesis(
                runtime.scene_id,
                SceneThesisCreate(
                    thesis_type=pulse.thesis.thesis_type,
                    text=f"Неразыгранное осложнение: {pulse.event}. {pulse.thesis.text}",
                    priority=pulse.thesis.priority,
                    visibility=pulse.thesis.visibility,
                    related_entity_ids=[
                        self.characters[name]
                        for name in pulse.thesis.related_names
                        if name in self.characters
                    ],
                ),
            )
            runtime.injected_pulses.add(pulse_index)
            self.stats["pulses_injected"] += 1
        await self.session.commit()

    async def confirm_pulses(
        self,
        runtime: PhaseRuntime,
        indexes: list[int],
        source_turn_id: UUID,
    ) -> None:
        for index in indexes:
            if index not in runtime.injected_pulses or index in runtime.confirmed_pulses:
                continue
            if index < 0 or index >= len(runtime.phase.pulses):
                continue
            pulse = runtime.phase.pulses[index]
            await self.events.create(
                self.campaign_id,
                EventCreate(
                    event_type="scenario_pulse",
                    description=pulse.event,
                    location_id=runtime.location_id,
                    importance="important",
                    participant_ids=list(runtime.active_characters.values()),
                    source_turns=[source_turn_id],
                ),
            )
            runtime.confirmed_pulses.add(index)
            self.stats["pulses_confirmed"] += 1
        await self.session.commit()


async def generate_player_decision(
    provider: LLMProvider,
    config,
    api_key: str | None,
    compiler: ContextCompiler,
    campaign_id: UUID,
    runtime: PhaseRuntime,
    player_id: UUID,
    history: list,
    policy: PlayerPolicy,
    turn_number: int,
    active_theses: list[str],
) -> PlayerDecision:
    active_npcs = list(runtime.phase.active_npcs)
    preferred = policy.preferred_mode(turn_number)
    suggested = policy.suggested_target(active_npcs, preferred)
    messages, _ = await compiler.compile_context(
        campaign_id=campaign_id,
        acting_character_id=player_id,
        scene_id=runtime.scene_id,
    )
    trusted_context = messages[0].content if messages else ""
    recent = "\n".join(
        f"{'ДМ' if turn.role == 'assistant' else 'ИГРОК'}: {turn.content}"
        for turn in history[-10:]
    )
    system = f"""Ты имитируешь живого игрока настольной RPG, а не второго ДМа.
Верни один JSON: {{"target":"narrator|ActiveNpc","mode":"action|dialogue|question|plan|decision","intent":"1-3 предложения"}}.

ЦЕЛЬ СЦЕНЫ: {runtime.phase.objective}
АКТИВНЫЕ NPC: {', '.join(active_npcs)}
ПРЕДПОЧТИТЕЛЬНЫЙ ТИП ХОДА: {preferred}
НЕДОИСПОЛЬЗОВАННЫЙ NPC: {suggested}
ТЕКУЩИЕ ТЕЗИСЫ: {' | '.join(active_theses[-8:])}

Правила:
- intent только на русском языке.
- Опиши только речь, вопрос, план, решение или попытку действия Элдона.
- Не объявляй успех, находку, урон, реакцию NPC, открытие двери или смену сцены.
- Реагируй на последний результат, а не на абстрактное «препятствие».
- Используй конкретный предмет, наблюдение или компетенцию, когда это уместно.
- После похожей попытки меняй подход.
- Не повторяй фразы из списка недавних действий.

ДОВЕРЕННЫЙ КОНТЕКСТ ЭЛДОНА:
{trusted_context}"""
    user = (
        f"НЕДАВНЯЯ ИГРА:\n{recent or '(начало сцены)'}\n\n"
        "НЕДАВНИЕ ДЕЙСТВИЯ, КОТОРЫЕ НЕЛЬЗЯ ПОВТОРЯТЬ:\n"
        + "\n".join(policy.recent_fingerprints)
    )
    error = None
    for _ in range(2):
        raw = ""
        correction = f"\nПредыдущий JSON отклонён: {error}." if error else ""
        try:
            async for token in provider.generate_stream(
                [
                    ChatMessage(role="system", content=system + correction),
                    ChatMessage(role="user", content=user),
                ],
                config,
                api_key,
                max_tokens=420,
                temperature=0.75,
            ):
                raw += token
            decision = parse_player_decision(raw, active_npcs)
            valid, error = policy.validate(decision, active_npcs)
            if valid:
                policy.remember(decision)
                return decision
        except (ValueError, ValidationError, LLMProviderError) as exc:
            error = str(exc)

    latest_result = next(
        (turn.content for turn in reversed(history) if turn.role == "assistant"),
        "",
    )
    decision = policy.fallback(
        active_npcs,
        preferred,
        runtime.phase.objective,
        latest_result,
        active_theses,
        turn_number,
    )
    policy.remember(decision)
    return decision


async def evaluate_objective(
    provider: LLMProvider,
    config,
    api_key: str | None,
    runtime: PhaseRuntime,
    recent_history: list,
    assistant_content: str,
    active_theses: list[str],
    minimum_turns: int,
) -> ObjectiveEvaluation:
    if runtime.phase_turn < minimum_turns:
        return ObjectiveEvaluation(
            status="progressing",
            evidence=f"Минимальная длина сцены ещё не достигнута: {runtime.phase_turn}/{minimum_turns}",
        )
    pending = [
        f"{index}: {runtime.phase.pulses[index].event}"
        for index in sorted(runtime.injected_pulses - runtime.confirmed_pulses)
    ]
    recent = "\n".join(
        f"{'ДМ' if turn.role == 'assistant' else 'ИГРОК'}: {turn.content}"
        for turn in recent_history[-8:]
    )
    prompt = f"""Ты проверяешь фактическое состояние цели сцены RPG.
Не оценивай качество прозы. Верни только JSON:
{{"status":"progressing|resolved|failed|blocked","evidence":"короткая цитата или факт","outcome_summary":"итог или null","confirmed_pulses":[индексы]}}

ЦЕЛЬ: {runtime.phase.objective}
ХОДОВ В СЦЕНЕ: {runtime.phase_turn}
АКТИВНЫЕ ТЕЗИСЫ: {' | '.join(active_theses)}
ОЖИДАЮЩИЕ ОСЛОЖНЕНИЯ:
{chr(10).join(pending) or '- нет'}

НЕДАВНЯЯ ИГРА:
{recent}

ПОСЛЕДНИЙ РЕЗУЛЬТАТ ДМА:
{assistant_content}

Resolved только если цель действительно достигнута в повествовании.
Failed только если цель стала невозможна или выбран явный провал.
Blocked, если нужен новый подход, но сцена ещё продолжается.
Не считай режиссёрский тезис или план уже случившимся событием.
"""
    raw = ""
    try:
        async for token in provider.generate_stream(
            [ChatMessage(role="system", content=prompt)],
            config,
            api_key,
            max_tokens=320,
            temperature=0.1,
        ):
            raw += token
        return ObjectiveEvaluation.model_validate(parse_json_object(raw))
    except Exception:
        return ObjectiveEvaluation(
            status="progressing",
            evidence="Evaluator недоступен; цель не считается выполненной без доказательства.",
        )


async def resolve_turn_proposals(
    session: AsyncSession,
    assistant_turn_id: UUID,
) -> tuple[list[str], list[str]]:
    repo = ProposedChangeRepository(session)
    accepted: list[str] = []
    rejected: list[str] = []
    for proposal in await repo.get_for_turn(assistant_turn_id):
        if proposal.status == "invalid":
            rejected.append(proposal.payload.get("_validation_error", "invalid proposal"))
            await repo.resolve(proposal.id, ProposalAction(status="rejected"))
            continue
        if proposal.status != "proposed":
            continue
        try:
            await resolve_proposal(
                proposal.id,
                ProposalAction(status="accepted"),
                session=session,
            )
            accepted.append(f"{proposal.change_type}: {proposal.payload}")
        except Exception as exc:
            await session.rollback()
            rejected.append(f"{proposal.change_type}: {exc}")
    await session.commit()
    return accepted, rejected


async def find_logical_pair(
    session: AsyncSession,
    campaign_id: UUID,
    run_id: str,
    logical_turn: int,
):
    result = await session.execute(
        select(DBTurn)
        .where(DBTurn.campaign_id == str(campaign_id))
        .order_by(DBTurn.created_at)
    )
    user = None
    assistant = None
    for row in result.scalars().all():
        if not row.context_snapshot:
            continue
        try:
            snapshot = json.loads(row.context_snapshot)
        except Exception:
            continue
        marker = snapshot.get("simulation") or {}
        if marker.get("run_id") != run_id or marker.get("logical_turn") != logical_turn:
            continue
        if row.role == "user":
            user = row
        elif row.role == "assistant":
            assistant = row
    if user and assistant is None:
        child = await session.execute(
            select(DBTurn)
            .where(
                DBTurn.parent_turn_id == user.id,
                DBTurn.role == "assistant",
                DBTurn.status == "active",
            )
            .order_by(DBTurn.created_at.desc())
        )
        assistant = child.scalars().first()
    return user, assistant


async def latest_assistant_for_user(session: AsyncSession, user_id: str):
    result = await session.execute(
        select(DBTurn)
        .where(DBTurn.parent_turn_id == user_id, DBTurn.role == "assistant")
        .order_by(DBTurn.created_at.desc())
    )
    return result.scalars().first()


async def count_campaign_rows(session, model, campaign_id: UUID) -> int:
    query = select(func.count()).select_from(model)
    if hasattr(model, "campaign_id"):
        query = query.where(model.campaign_id == str(campaign_id))
    elif model is SceneThesis:
        query = query.join(Scene, Scene.id == SceneThesis.scene_id).where(
            Scene.campaign_id == str(campaign_id)
        )
    return int((await session.execute(query)).scalar_one())


async def run_realistic_simulation_v2() -> None:
    data_dir = Path(os.getenv("PDM_SIM_DATA_DIR", "./data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    database_path = Path(os.getenv("PDM_SIM_DB", str(data_dir / "realistic_simulation.db")))
    log_path = data_dir / "realistic_simulation_play.log"
    trace_path = data_dir / "realistic_simulation_trace.jsonl"
    report_path = data_dir / "realistic_simulation_report.md"
    state_path = data_dir / "realistic_simulation_state.json"

    should_reset = os.getenv("PDM_SIM_RESET", "1") == "1"
    if should_reset:
        for path in (
            database_path,
            database_path.with_suffix(database_path.suffix + "-wal"),
            database_path.with_suffix(database_path.suffix + "-shm"),
            log_path,
            trace_path,
            report_path,
            state_path,
        ):
            if path.exists():
                path.unlink()

    turns_limit = max(20, int(os.getenv("PDM_SIM_TURNS", "200")))
    model_name = os.getenv("PDM_SIM_MODEL", "gemma4:e4b")
    base_url = os.getenv("PDM_SIM_BASE_URL", "http://127.0.0.1:11434/v1")
    context_window = int(os.getenv("PDM_SIM_CONTEXT_WINDOW", "8192"))
    stop_on_failure = os.getenv("PDM_SIM_STOP_ON_PROVIDER_FAILURE", "1") == "1"
    phase_budget = max(10, turns_limit // len(PHASES))
    minimum_phase_turns = max(4, phase_budget // 3)
    hard_phase_limit = phase_budget + 4

    state = SimulationState.load(state_path)
    if not state:
        state = SimulationState(run_id=os.getenv("PDM_SIM_RUN_ID", str(uuid4())))
    trace = TraceStore(trace_path)

    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with factory() as session:
        campaigns = CampaignService(session)
        campaign_repo = CampaignRepository(session)
        existing_campaigns = await campaign_repo.list_all()
        campaign = None
        if state.campaign_id:
            campaign = await campaign_repo.get_by_id(UUID(state.campaign_id))
        if not campaign and existing_campaigns and not should_reset:
            campaign = next(
                (
                    item
                    for item in existing_campaigns
                    if item.name == "Хроники Бездны: реалистичная автономная кампания"
                ),
                None,
            )
        if not campaign:
            campaign = await campaigns.create_campaign(
                CampaignCreate(
                    name="Хроники Бездны: реалистичная автономная кампания",
                    description="Objective-driven LLM-vs-LLM benchmark with idempotent resume.",
                    system_instructions=(
                        "Ты приземлённый Dungeon Master в жанре тёмного фэнтези. "
                        "Пиши исключительно на русском языке. Игрок заявляет только намерения; "
                        "ты определяешь исходы и даёшь конкретные последствия. Уважай карточки NPC, "
                        "инвентарь, способности, знания и тезисы. Не говори за Элдона, не повторяй "
                        "универсальные формулы о риске и не заменяй действие новым вопросом."
                    ),
                    narrative_style=(
                        "Компактная романная проза, конкретные сенсорные детали, различимые голоса NPC "
                        "и завершённый результат каждого хода на русском языке."
                    ),
                )
            )
        state.campaign_id = str(campaign.id)
        state.save(state_path)
        campaign_id = campaign.id

        config = await campaigns.configure_provider(
            campaign_id,
            ProviderConfigCreate(
                base_url=base_url,
                model_name=model_name,
                context_window=context_window,
            ),
        )
        config_repo = ProviderConfigRepository(session)
        api_key = await config_repo.get_decrypted_key(campaign_id)
        role_router = RoleModelRouter(config_repo)
        builder_selection = await role_router.resolve(
            campaign_id,
            ModelRole.CHARACTER_BUILDER,
            config,
        )
        evaluator_selection = await role_router.resolve(
            campaign_id,
            ModelRole.EVALUATOR,
            config,
        )
        if builder_selection is None or evaluator_selection is None:
            raise RuntimeError("Role model routing requires a configured campaign provider")
        entities = EntityRepository(session)
        characters = await entities.list_by_campaign(campaign_id, "character")
        player_entity = next(
            (entity for entity in characters if entity.canonical_name == "Eldon"),
            None,
        )
        if not player_entity:
            player = await create_character_from_draft(
                campaign_id,
                eldon_card(),
                session=session,
            )
            player_id = player.character.id
        else:
            player_id = player_entity.id

        if campaign.player_character_id != player_id:
            campaign = await campaigns.update_campaign(
                campaign_id, CampaignUpdate(player_character_id=player_id)
            )
            await session.commit()

        provider = LLMProvider()
        stats: Counter = Counter()
        director = ScenarioDirector(
            session,
            campaign_id,
            player_id,
            provider,
            builder_selection.config,
            builder_selection.api_key,
            stats,
        )
        await director.restore_characters()
        runner = TurnRunner(session)
        compiler = ContextCompiler(session)
        turns = TurnRepository(session)
        scenes = SceneRepository(session)
        policy = PlayerPolicy()
        started = time.time()

        while (
            state.logical_turn <= turns_limit
            and state.phase_index < len(PHASES)
            and not state.completed
        ):
            runtime = await director.enter_phase(state.phase_index, state)
            runtime.phase_turn = state.phase_turn
            runtime.injected_pulses = set(state.injected_pulses)
            runtime.confirmed_pulses = set(state.confirmed_pulses)
            await director.inject_due_pulses(runtime, hard_phase_limit)

            active_theses_rows = await scenes.list_theses_by_scene(
                runtime.scene_id,
                active_only=True,
            )
            active_thesis_texts = [item.text for item in active_theses_rows]
            history = await turns.get_history(campaign_id, limit=20, active_only=True)

            existing_user, existing_assistant = await find_logical_pair(
                session,
                campaign_id,
                state.run_id,
                state.logical_turn,
            )

            if existing_assistant and existing_assistant.content.lstrip().startswith("[Generation failed"):
                print(f"[simulation] Deleting failed logical turn {state.logical_turn} from DB to re-attempt.")
                from sqlalchemy import delete as sql_delete
                await session.execute(sql_delete(DBTurn).where(DBTurn.id == existing_assistant.id))
                if existing_user:
                    await session.execute(sql_delete(DBTurn).where(DBTurn.id == existing_user.id))
                await session.commit()
                existing_user = None
                existing_assistant = None
                state.consecutive_failures = 0
                state.save(state_path)

            if existing_assistant and existing_assistant.status == "active":
                decision = PlayerDecision(
                    target="narrator",
                    mode="action",
                    intent="Восстановленный после сбоя ход; исход уже сохранён в БД.",
                )
                dm_text = existing_assistant.content
                assistant_turn_id = UUID(existing_assistant.id)
                accepted: list[str] = []
                rejected: list[str] = []
            else:
                decision = await generate_player_decision(
                    provider,
                    config,
                    api_key,
                    compiler,
                    campaign_id,
                    runtime,
                    player_id,
                    history,
                    policy,
                    state.logical_turn,
                    active_thesis_texts,
                )
                player_text = decision.render()
                actor_id = (
                    runtime.active_characters.get(decision.target)
                    if decision.target != "narrator"
                    else None
                )
                simulation_marker = {
                    "run_id": state.run_id,
                    "logical_turn": state.logical_turn,
                    "phase_index": state.phase_index,
                    "phase_slug": runtime.phase.slug,
                }
                existing_user_id = None
                if existing_user:
                    existing_user.status = "active"
                    existing_user_id = UUID(existing_user.id)
                    await session.flush()
                dm_text = ""
                async for token in runner.run_turn_stream(
                    campaign_id,
                    TurnCreate(
                        role="user",
                        content=player_text,
                        scene_id=runtime.scene_id,
                        acting_character_id=actor_id,
                        context_snapshot={"simulation": simulation_marker},
                    ),
                    existing_user_turn_id=existing_user_id,
                ):
                    dm_text += token
                await session.commit()
                user_row, assistant_row = await find_logical_pair(
                    session,
                    campaign_id,
                    state.run_id,
                    state.logical_turn,
                )
                if assistant_row is None and user_row is not None:
                    assistant_row = await latest_assistant_for_user(session, user_row.id)
                assistant_turn_id = UUID(assistant_row.id) if assistant_row else None
                if dm_text.lstrip().startswith("[Generation failed") or not assistant_turn_id:
                    state.consecutive_failures += 1
                    stats["generation_failures"] += 1
                    trace.upsert(
                        {
                            "turn": state.logical_turn,
                            "run_id": state.run_id,
                            "phase": runtime.phase.slug,
                            "phase_title": runtime.phase.title,
                            "objective": runtime.phase.objective,
                            "active_npcs": list(runtime.phase.active_npcs),
                            "player": asdict(decision),
                            "dm": dm_text.strip(),
                            "generation_failed": True,
                            "active_theses": [
                                {
                                    "id": str(item.id),
                                    "type": item.thesis_type,
                                    "text": item.text,
                                    "visibility": item.visibility,
                                }
                                for item in active_theses_rows
                            ],
                        }
                    )
                    trace.write_play_log(log_path, turns_limit)
                    state.injected_pulses = sorted(runtime.injected_pulses)
                    state.confirmed_pulses = sorted(runtime.confirmed_pulses)
                    state.save(state_path)
                    if stop_on_failure:
                        print(
                            f"[simulation] Provider failure on logical turn {state.logical_turn}; "
                            "state saved for idempotent resume."
                        )
                        break
                    state.logical_turn += 1
                    state.save(state_path)
                    continue

                state.consecutive_failures = 0
                accepted, rejected = await resolve_turn_proposals(
                    session,
                    assistant_turn_id,
                )

            active_theses_rows = await scenes.list_theses_by_scene(
                runtime.scene_id,
                active_only=True,
            )
            active_thesis_texts = [item.text for item in active_theses_rows]
            recent_history = await turns.get_history(campaign_id, limit=12, active_only=True)
            evaluation = await evaluate_objective(
                provider,
                evaluator_selection.config,
                evaluator_selection.api_key,
                runtime,
                recent_history,
                dm_text,
                active_thesis_texts,
                minimum_phase_turns,
            )
            if assistant_turn_id:
                await director.confirm_pulses(
                    runtime,
                    evaluation.confirmed_pulses,
                    assistant_turn_id,
                )

            force_close = runtime.phase_turn + 1 >= hard_phase_limit
            phase_finished = evaluation.status in {"resolved", "failed"} or force_close
            if force_close and evaluation.status not in {"resolved", "failed"}:
                evaluation = ObjectiveEvaluation(
                    status="failed",
                    evidence="Достигнут жёсткий лимит сцены без подтверждённого достижения цели.",
                    outcome_summary=(
                        "Группа покидает сцену с незавершённой целью; следующая сцена получает "
                        "явное последствие этого провала."
                    ),
                    confirmed_pulses=evaluation.confirmed_pulses,
                )

            record = {
                "turn": state.logical_turn,
                "run_id": state.run_id,
                "phase": runtime.phase.slug,
                "phase_title": runtime.phase.title,
                "phase_turn": runtime.phase_turn + 1,
                "objective": runtime.phase.objective,
                "active_npcs": list(runtime.phase.active_npcs),
                "player": asdict(decision),
                "dm": dm_text.strip(),
                "generation_failed": False,
                "accepted": accepted,
                "rejected": rejected,
                "evaluation": evaluation.model_dump(),
                "active_theses": [
                    {
                        "id": str(item.id),
                        "type": item.thesis_type,
                        "text": item.text,
                        "visibility": item.visibility,
                    }
                    for item in active_theses_rows
                ],
            }
            trace.upsert(record)
            trace.write_play_log(log_path, turns_limit)

            state.logical_turn += 1
            runtime.phase_turn += 1
            state.phase_turn = runtime.phase_turn
            state.injected_pulses = sorted(runtime.injected_pulses)
            state.confirmed_pulses = sorted(runtime.confirmed_pulses)

            if phase_finished:
                await director.close_current(
                    evaluation.status,
                    evaluation.outcome_summary or evaluation.evidence,
                    assistant_turn_id,
                )
                state.phase_index += 1
                state.phase_turn = 0
                state.injected_pulses = []
                state.confirmed_pulses = []
                director.current = None
                if state.phase_index >= len(PHASES):
                    state.completed = True
            state.save(state_path)

            if state.logical_turn % 5 == 0:
                print(
                    f"[{state.logical_turn - 1}/{turns_limit}] phase={runtime.phase.slug}; "
                    f"phase_turn={runtime.phase_turn}; status={evaluation.status}; "
                    f"{(time.time() - started) / max(1, state.logical_turn - 1):.2f}s/turn"
                )

        all_turns = await turns.get_history(campaign_id, limit=turns_limit * 4, active_only=False)
        active_theses = await session.execute(
            select(func.count()).select_from(SceneThesis).where(SceneThesis.status == "active")
        )
        completed_scene_active_theses = await session.execute(
            select(func.count())
            .select_from(SceneThesis)
            .join(Scene, Scene.id == SceneThesis.scene_id)
            .where(Scene.status == "completed", SceneThesis.status == "active")
        )
        proposal_counts = await session.execute(
            select(ProposedChange.status, func.count()).group_by(ProposedChange.status)
        )
        proposal_summary = dict(proposal_counts.all())
        counts = {
            "entities": await count_campaign_rows(session, Entity, campaign_id),
            "events": await count_campaign_rows(session, Event, campaign_id),
            "relationships": await count_campaign_rows(session, RelationshipAssertion, campaign_id),
            "thesis_versions": await count_campaign_rows(session, SceneThesis, campaign_id),
            "active_theses": int(active_theses.scalar_one()),
            "active_theses_in_completed_scenes": int(completed_scene_active_theses.scalar_one()),
            "beliefs": int((await session.execute(select(func.count()).select_from(Belief))).scalar_one()),
            "goals": int((await session.execute(select(func.count()).select_from(CharacterGoal))).scalar_one()),
            "facts": len(await FactRepository(session).list_active(campaign_id)),
            "accepted_proposals": int(proposal_summary.get("accepted", 0)),
            "rejected_proposals": int(proposal_summary.get("rejected", 0)),
            "invalid_proposals": int(proposal_summary.get("invalid", 0)),
        }
        logical_records = list(trace.records.values())
        generation_failures = sum(bool(item.get("generation_failed")) for item in logical_records)
        builder_total = sum(
            stats[key]
            for key in ("character_builder_model", "character_builder_repair", "character_builder_fallback")
        )
        lines = [
            "# Отчёт о реалистичной автономной кампании v2",
            "",
            f"- Run ID: `{state.run_id}`",
            f"- Кампания: {campaign.name}",
            f"- Запланированный предел ходов: {turns_limit}",
            f"- Уникальных логических ходов: {len(logical_records)}",
            f"- Следующий логический ход: {state.logical_turn}",
            f"- Кампания завершена: {state.completed}",
            f"- Пройдено фаз: {state.phase_index}/{len(PHASES)}",
            f"- Строк turns в SQLite: {len(all_turns)}",
            f"- Ошибок генерации в уникальных ходах: {generation_failures}",
            f"- Character Builder model/repair/fallback: {stats['character_builder_model']}/{stats['character_builder_repair']}/{stats['character_builder_fallback']}",
            f"- Character Builder fallback rate: {(stats['character_builder_fallback'] / builder_total * 100 if builder_total else 0):.1f}%",
            f"- Player fallbacks: {policy.fallbacks}",
            f"- Отклонено player-outcomes: {policy.rejected_outcomes}",
            f"- Отклонено повторов: {policy.repeated_actions}",
            f"- Phrases unique: {len({policy.fingerprint(item.get('player', {}).get('intent', '')) for item in logical_records})}",
            *(f"- {name}: {value}" for name, value in counts.items()),
            f"- Время текущего запуска: {(time.time() - started) / 60:.2f} минут",
            "",
            "## Поведение игрока",
            *(f"- {mode}: {policy.mode_counts[mode]}" for mode in PlayerPolicy.MODES),
            "",
            f"- SQLite: `{database_path}`",
            f"- State: `{state_path}`",
            f"- Лог: `{log_path}`",
            f"- JSONL: `{trace_path}`",
        ]
        report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        await session.commit()
        await session.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))

    await engine.dispose()
    print("=== REALISTIC AUTONOMOUS CAMPAIGN V2 FINISHED ===")


if __name__ == "__main__":
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run_realistic_simulation_v2())
