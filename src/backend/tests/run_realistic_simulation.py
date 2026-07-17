from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections import Counter, deque
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.memory import resolve_proposal
from app.api.world_state import (
    CharacterDraft,
    CharacterDraftRequest,
    create_character_from_draft,
    draft_character,
)
from app.db.engine import Base
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.event_repo import EventRepository
from app.db.repositories.fact_repo import FactRepository
from app.db.repositories.proposed_change_repo import ProposedChangeRepository
from app.db.repositories.provider_config_repo import ProviderConfigRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.repositories.turn_repo import TurnRepository
from app.db.tables import Belief, CharacterGoal, Entity, Event, RelationshipAssertion, Scene, SceneThesis
from app.models.campaign import CampaignCreate, CampaignUpdate
from app.models.character import CharacterUpdate
from app.models.entity import EntityCreate, EntityType
from app.models.event import EventCreate
from app.models.proposed_change import ProposalAction
from app.models.provider_config import ProviderConfigCreate
from app.models.scene import SceneCreate, SceneUpdate
from app.models.scene_thesis import SceneThesisCreate, ThesisType
from app.models.turn import ChatMessage, TurnCreate
from app.providers.llm_provider import LLMProvider, LLMProviderError
from app.services.campaign_service import CampaignService
from app.services.context_compiler import ContextCompiler
from app.services.turn_runner import TurnRunner

try:
    from .simulation_scenario import NPCS, PHASES, NpcConcept, ScenarioPhase, phase_index_for_turn, phase_progress
except ImportError:
    from simulation_scenario import NPCS, PHASES, NpcConcept, ScenarioPhase, phase_index_for_turn, phase_progress


PLAYER_MODES = ("question", "action", "dialogue", "action", "plan", "action", "dialogue", "decision")
OUTCOME_PATTERNS = (
    r"\bI (?:successfully|definitely|immediately)\b",
    r"\bI (?:discover|find|reveal|prove|confirm|unlock|open|destroy|kill|defeat|heal|solve)\b",
    r"\bthe (?:door|gate|lock|mechanism|enemy|creature|ritual) (?:opens|breaks|dies|fails|activates|reveals)\b",
    r"\bmy (?:test|spell|attack|plan) (?:works|succeeds)\b",
)


@dataclass(frozen=True)
class PlayerDecision:
    target: str
    mode: str
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
    fired_pulses: set[int]


class PlayerPolicy:
    def __init__(self) -> None:
        self.recent_intents: deque[str] = deque(maxlen=8)
        self.mode_counts: Counter[str] = Counter()
        self.target_counts: Counter[str] = Counter()
        self.rejected_outcomes = 0
        self.repeated_actions = 0

    @staticmethod
    def fingerprint(value: str) -> str:
        return re.sub(r"[^a-z0-9 ]+", "", value.casefold()).strip()

    def preferred_mode(self, turn_number: int) -> str:
        return PLAYER_MODES[(turn_number - 1) % len(PLAYER_MODES)]

    def suggested_target(self, active_npcs: list[str], mode: str) -> str:
        if mode in {"question", "dialogue", "plan", "decision"} and active_npcs:
            return min(active_npcs, key=lambda name: (self.target_counts[name.casefold()], name.casefold()))
        return "narrator"

    def validate(self, decision: PlayerDecision, active_npcs: list[str]) -> tuple[bool, str | None]:
        active = {name.casefold() for name in active_npcs}
        if decision.target.casefold() != "narrator" and decision.target.casefold() not in active:
            return False, f"target {decision.target!r} is not active"
        if decision.mode not in {"action", "dialogue", "question", "plan", "decision"}:
            return False, f"unsupported mode {decision.mode!r}"
        if not decision.intent.strip() or len(decision.intent) > 700:
            return False, "intent is empty or too long"
        if any(re.search(pattern, decision.intent, re.IGNORECASE) for pattern in OUTCOME_PATTERNS):
            self.rejected_outcomes += 1
            return False, "intent declares an outcome"
        fingerprint = self.fingerprint(decision.intent)
        if fingerprint and fingerprint in self.recent_intents:
            self.repeated_actions += 1
            return False, "intent repeats a recent action"
        return True, None

    def remember(self, decision: PlayerDecision) -> None:
        self.recent_intents.append(self.fingerprint(decision.intent))
        self.mode_counts[decision.mode] += 1
        self.target_counts[decision.target.casefold()] += 1

    def fallback(self, active_npcs: list[str], mode: str) -> PlayerDecision:
        target = self.suggested_target(active_npcs, mode)
        variants = {
            "question": f'"Какую конкретную опасность ты здесь видишь, {target}?"',
            "dialogue": f'Я говорю {target}: "Мне нужно твое честное мнение, прежде чем мы продолжим."',
            "plan": "Я сопоставляю риски вслух и предлагаю группе осторожный следующий шаг.",
            "decision": "Я выбираю наименее разрушительный вариант и прошу группу высказать последнее возражение.",
            "action": "Я осматриваю препятствие, не прикасаясь к нему, и ищу безопасный проход.",
        }
        if target == "narrator" and mode in {"question", "dialogue"}:
            mode = "action"
        return PlayerDecision(target=target, mode=mode, intent=variants[mode])


def parse_player_decision(raw: str, active_npcs: list[str]) -> PlayerDecision:
    clean = raw.strip()
    if clean.startswith("```"):
        clean = "\n".join(clean.splitlines()[1:-1]).strip()
    data = json.loads(clean)
    if not isinstance(data, dict):
        raise ValueError("player response must be an object")
    target = str(data.get("target", "narrator")).strip()
    canonical = {name.casefold(): name for name in active_npcs}
    target = canonical.get(target.casefold(), "narrator" if target.casefold() == "narrator" else target)
    return PlayerDecision(
        target=target,
        mode=str(data.get("mode", "action")).strip().casefold(),
        intent=str(data.get("intent", "")).strip(),
    )


def eldon_card() -> CharacterDraft:
    return CharacterDraft(
        canonical_name="Eldon",
        description="A practical human ruin-delver who survives through caution and cooperation.",
        appearance="Weathered travel coat, close-cropped dark hair and a scar across the right eyebrow.",
        personality="Practical, suspicious of easy answers, terse and willing to listen.",
        values=["keeping companions alive", "evidence", "freedom to choose"],
        fears=["being manipulated", "losing people through reckless curiosity"],
        desires=["understand the citadel", "leave with the relic key and a living party"],
        voice="Low, direct and dryly humorous.",
        speech_patterns="Asks concrete questions and names risks before acting.",
        biography="A caravan guard and ruin-delver experienced with mundane traps.",
        backstory_public="A vanished patron selected him as a contingency explorer.",
        secrets=["Eldon suspects the patron deliberately used him as a disposable backup."],
        emotional_state="guarded curiosity",
        current_intentions=["learn who can be trusted", "find the relic key"],
        goals=["Find the relic key", "Keep the expedition alive", "Learn why the patron chose Eldon"],
        capabilities=["inspect mundane mechanisms", "use basic lockpicks", "fight with a dagger", "climb with rope", "notice practical danger"],
        limitations=["cannot cast magic", "cannot identify supernatural effects alone", "cannot use advanced technology", "cannot declare outcomes"],
        equipment=["traveller's torch", "hemp rope", "plain dagger", "basic lockpick roll", "waterskin"],
        initial_beliefs=["Every source describing the relic key is incomplete."],
        visual_profile={"palette": "brown, iron and weathered green"},
    )


def fallback_npc_card(seed: NpcConcept, location_id: UUID) -> CharacterDraft:
    return CharacterDraft(
        canonical_name=seed.name,
        description=f"A travelling specialist serving as {seed.campaign_role}.",
        appearance="Practical expedition clothing with one distinctive personal detail.",
        personality=seed.tone,
        values=["survival", "professional competence"],
        fears=["being exposed before the mission is complete"],
        desires=["complete the expedition objective"],
        voice="Distinct but restrained.",
        speech_patterns="Uses concrete observations and avoids speeches.",
        biography=f"Joined the expedition as {seed.campaign_role}.",
        backstory_public=f"Known to the group as {seed.campaign_role}.",
        secrets=[seed.concept],
        emotional_state="alert",
        current_intentions=["assess Eldon", "contribute without revealing too much"],
        goals=["advance the expedition", "protect a private truth"],
        capabilities=["apply their stated expedition expertise"],
        limitations=["cannot use unlisted powers or equipment"],
        equipment=["ordinary travel gear", "tools appropriate to their role"],
        initial_beliefs=["The citadel is more dangerous than its public history admits."],
        visual_profile={"role": seed.campaign_role, "tone": seed.tone},
        current_location_id=location_id,
    )


class ScenarioDirector:
    def __init__(self, session, campaign_id: UUID, player_id: UUID):
        self.session = session
        self.campaign_id = campaign_id
        self.player_id = player_id
        self.campaign_service = CampaignService(session)
        self.entities = EntityRepository(session)
        self.scenes = SceneRepository(session)
        self.events = EventRepository(session)
        self.characters: dict[str, UUID] = {"Eldon": player_id}
        self.current: PhaseRuntime | None = None
        self.transitions = 0
        self.pulses_fired = 0
        self.npcs_introduced = 0

    async def ensure_npc(self, name: str, location_id: UUID) -> UUID:
        if name in self.characters:
            return self.characters[name]
        seed = NPCS[name]
        try:
            card = await draft_character(
                self.campaign_id,
                CharacterDraftRequest(
                    name=seed.name,
                    concept=seed.concept,
                    campaign_role=seed.campaign_role,
                    tone=seed.tone,
                    current_location_id=location_id,
                ),
                session=self.session,
            )
        except Exception as exc:
            print(f"[director] Character Builder failed for {name}: {exc}; using fallback")
            card = fallback_npc_card(seed, location_id)
        built = await create_character_from_draft(
            self.campaign_id,
            card.model_copy(update={"current_location_id": location_id}),
            session=self.session,
        )
        self.characters[name] = built.character.id
        self.npcs_introduced += 1
        return built.character.id

    async def enter_phase(self, index: int) -> PhaseRuntime:
        phase = PHASES[index]
        if self.current:
            await self.scenes.update(self.current.scene_id, SceneUpdate(status="completed"))
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
        scene = await self.scenes.create(
            self.campaign_id,
            SceneCreate(
                title=phase.title,
                location_description=phase.location_description,
                mood=phase.mood,
                tension=phase.tension,
            ),
        )
        active = {name: self.characters[name] for name in phase.active_npcs}
        active["Eldon"] = self.player_id
        for character_id in active.values():
            await self.scenes.add_participant(scene.id, character_id)
            await self.entities.update_character(character_id, CharacterUpdate(current_location_id=location.id))
        await self.campaign_service.update_campaign(self.campaign_id, CampaignUpdate(current_scene_id=scene.id))
        await self.scenes.create_thesis(
            scene.id,
            SceneThesisCreate(
                thesis_type=ThesisType.INTENTION,
                text=f"Scenario direction: {phase.director_note}",
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
                    related_entity_ids=[self.characters[name] for name in seed.related_names if name in self.characters],
                ),
            )
        await self.events.create(
            self.campaign_id,
            EventCreate(
                event_type="scene_transition",
                description=f"The expedition entered {phase.title}. Objective: {phase.objective}",
                location_id=location.id,
                importance="important",
                participant_ids=list(active.values()),
            ),
        )
        await self.session.commit()
        self.transitions += 1
        self.current = PhaseRuntime(index, phase, scene.id, location.id, active, set())
        return self.current

    async def ensure_turn(self, turn_number: int, total_turns: int) -> PhaseRuntime:
        index = phase_index_for_turn(turn_number, total_turns)
        if not self.current or self.current.index != index:
            await self.enter_phase(index)
        assert self.current
        progress = phase_progress(turn_number, total_turns, index)
        for pulse_index, pulse in enumerate(self.current.phase.pulses):
            if pulse_index in self.current.fired_pulses or progress < pulse.at_fraction:
                continue
            await self.events.create(
                self.campaign_id,
                EventCreate(
                    event_type="scenario_pulse",
                    description=pulse.event,
                    location_id=self.current.location_id,
                    importance="important",
                    participant_ids=list(self.current.active_characters.values()),
                ),
            )
            await self.scenes.create_thesis(
                self.current.scene_id,
                SceneThesisCreate(
                    thesis_type=pulse.thesis.thesis_type,
                    text=pulse.thesis.text,
                    priority=pulse.thesis.priority,
                    visibility=pulse.thesis.visibility,
                    related_entity_ids=[self.characters[name] for name in pulse.thesis.related_names if name in self.characters],
                ),
            )
            self.current.fired_pulses.add(pulse_index)
            self.pulses_fired += 1
        await self.session.commit()
        return self.current


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
        f"{'DM' if turn.role == 'assistant' else 'PLAYER'}: {turn.content}"
        for turn in history[-12:]
    )
    system = f"""You simulate a real tabletop RPG player, not a novelist and not a co-GM.
Return exactly one JSON object: {{"target":"narrator|ActiveNpc","mode":"action|dialogue|question|plan|decision","intent":"one to three sentences"}}.

CRITICAL RULE: Write the 'intent' field strictly in RUSSIAN language. Eldon speaks, thinks, and acts in Russian.

OBJECTIVE: {runtime.phase.objective}
ACTIVE NPCS: {', '.join(active_npcs)}
PREFERRED MODE: {preferred}
UNDERUSED TARGET: {suggested}

Rules:
- Describe only Eldon's speech, question, plan, decision or attempted action.
- Write the 'intent' field strictly in Russian language.
- Never declare success, discoveries, damage, reactions, opened doors or scene changes.
- Use only knowledge, abilities and inventory in Eldon's trusted context.
- React to the latest meaningful development and do not repeat recent actions.
- Ask NPCs for expertise, negotiate, retreat, rest or change approach when sensible.
- After two similar attempts, choose a different approach.
- target is narrator or exactly one active NPC. Do not use [/talk] tags in intent.

TRUSTED ELDON CONTEXT:
{trusted_context}"""
    user = f"RECENT PLAY:\n{recent or '(opening)'}\n\nRECENT ACTIONS TO AVOID:\n" + "\n".join(policy.recent_intents)
    error = None
    for _ in range(2):
        raw = ""
        correction = f"\nPrevious answer was rejected: {error}." if error else ""
        try:
            async for token in provider.generate_stream(
                [ChatMessage(role="system", content=system + correction), ChatMessage(role="user", content=user)],
                config,
                api_key,
            ):
                raw += token
            decision = parse_player_decision(raw, active_npcs)
            valid, error = policy.validate(decision, active_npcs)
            if valid:
                policy.remember(decision)
                return decision
        except (ValueError, json.JSONDecodeError, LLMProviderError) as exc:
            error = str(exc)
    decision = policy.fallback(active_npcs, preferred)
    policy.remember(decision)
    return decision


async def resolve_turn_proposals(session, assistant_turn_id: UUID) -> tuple[list[str], list[str]]:
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


async def count_campaign_rows(session, model, campaign_id: UUID) -> int:
    query = select(func.count()).select_from(model)
    if hasattr(model, "campaign_id"):
        query = query.where(model.campaign_id == str(campaign_id))
    elif model is SceneThesis:
        query = query.join(Scene, Scene.id == SceneThesis.scene_id).where(Scene.campaign_id == str(campaign_id))
    return int((await session.execute(query)).scalar_one())


async def run_realistic_simulation() -> None:
    data_dir = Path(os.getenv("PDM_SIM_DATA_DIR", "./data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    database_path = Path(os.getenv("PDM_SIM_DB", str(data_dir / "realistic_simulation.db")))
    log_path = data_dir / "realistic_simulation_play.log"
    trace_path = data_dir / "realistic_simulation_trace.jsonl"
    report_path = data_dir / "realistic_simulation_report.md"
    if os.getenv("PDM_SIM_RESET", "1") == "1" and database_path.exists():
        database_path.unlink()

    turns_count = max(20, int(os.getenv("PDM_SIM_TURNS", "1000")))
    model_name = os.getenv("PDM_SIM_MODEL", "gemma4:e4b")
    base_url = os.getenv("PDM_SIM_BASE_URL", "http://127.0.0.1:11434/v1")
    context_window = int(os.getenv("PDM_SIM_CONTEXT_WINDOW", "8192"))

    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with factory() as session:
        campaigns = CampaignService(session)
        campaign = await campaigns.create_campaign(
            CampaignCreate(
                name="Хроники Бездны: реалистичная автономная кампания",
                description="Staged LLM-vs-LLM benchmark with gradual NPC entry and living theses.",
                system_instructions=(
                    "Ты приземленный Dungeon Master в жанре темного фэнтези. Описывай мир и веди диалоги "
                    "исключительно на РУССКОМ языке. Игрок заявляет только намерения; ты решаешь исходы. Уважай "
                    "карты NPC, инвентарь, способности, тайные знания и тезисы сцены. Используй мало активных NPC, "
                    "продвигай сюжет решительно, закрывай разрешенные вопросы и не выдумывай бесконечные коридоры."
                ),
                narrative_style="Компактная проза, конкретные сенсорные детали и целенаправленные диалоги строго на РУССКОМ языке.",
            )
        )
        campaign_id = campaign.id
        config = await campaigns.configure_provider(
            campaign_id,
            ProviderConfigCreate(base_url=base_url, model_name=model_name, context_window=context_window),
        )
        api_key = await ProviderConfigRepository(session).get_decrypted_key(campaign_id)
        player = await create_character_from_draft(campaign_id, eldon_card(), session=session)
        player_id = player.character.id

        director = ScenarioDirector(session, campaign_id, player_id)
        runner = TurnRunner(session)
        compiler = ContextCompiler(session)
        provider = LLMProvider()
        turns = TurnRepository(session)
        scenes = SceneRepository(session)
        policy = PlayerPolicy()
        stats = Counter()
        thesis_counts: list[int] = []
        started = time.time()
        log_path.write_text("REALISTIC AUTONOMOUS CAMPAIGN\n\n", encoding="utf-8")
        trace_path.write_text("", encoding="utf-8")

        for number in range(1, turns_count + 1):
            runtime = await director.ensure_turn(number, turns_count)
            history = await turns.get_history(campaign_id, limit=12, active_only=True)
            decision = await generate_player_decision(
                provider, config, api_key, compiler, campaign_id, runtime, player_id, history, policy, number
            )
            player_text = decision.render()
            actor_id = runtime.active_characters.get(decision.target) if decision.target != "narrator" else None
            dm_text = ""
            async for token in runner.run_turn_stream(
                campaign_id,
                TurnCreate(role="user", content=player_text, scene_id=runtime.scene_id, acting_character_id=actor_id),
            ):
                dm_text += token
            await session.commit()

            accepted: list[str] = []
            rejected: list[str] = []
            if dm_text.startswith("[Generation failed") or not dm_text.strip():
                stats["generation_failures"] += 1
            else:
                latest = await turns.get_history(campaign_id, limit=1, active_only=True)
                if latest and latest[-1].role == "assistant":
                    accepted, rejected = await resolve_turn_proposals(session, latest[-1].id)
            stats["accepted"] += len(accepted)
            stats["rejected"] += len(rejected)
            active_theses = await scenes.list_theses_by_scene(runtime.scene_id, active_only=True)
            thesis_counts.append(len(active_theses))

            record = {
                "turn": number,
                "phase": runtime.phase.slug,
                "objective": runtime.phase.objective,
                "active_npcs": list(runtime.phase.active_npcs),
                "player": {"target": decision.target, "mode": decision.mode, "intent": decision.intent},
                "dm": dm_text.strip(),
                "accepted": accepted,
                "rejected": rejected,
                "active_theses": [
                    {"id": str(item.id), "type": item.thesis_type, "text": item.text, "visibility": item.visibility}
                    for item in active_theses
                ],
            }
            with trace_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write("=" * 72 + "\n")
                handle.write(f"TURN {number}/{turns_count} | {runtime.phase.title}\n")
                handle.write(f"OBJECTIVE: {runtime.phase.objective}\n")
                handle.write(f"ACTIVE NPCS: {', '.join(runtime.phase.active_npcs)}\n")
                handle.write(f"PLAYER [{decision.mode} -> {decision.target}]: {decision.intent}\n")
                handle.write("-" * 72 + "\n")
                handle.write(f"DM: {dm_text.strip()}\n")
                for item in accepted:
                    handle.write(f"ACCEPTED: {item}\n")
                for item in rejected:
                    handle.write(f"REJECTED: {item}\n")
                handle.write("THESES:\n")
                for item in active_theses:
                    handle.write(f" - [{item.thesis_type}] {item.text}\n")
                handle.write("\n")

            if number % 10 == 0:
                print(
                    f"[{number}/{turns_count}] phase={runtime.phase.slug}; "
                    f"{(time.time() - started) / number:.2f}s/turn; theses={len(active_theses)}; "
                    f"failures={stats['generation_failures']}"
                )

        all_turns = await turns.get_history(campaign_id, limit=turns_count * 3, active_only=False)
        average_theses = sum(thesis_counts) / len(thesis_counts) if thesis_counts else 0
        counts = {
            "entities": await count_campaign_rows(session, Entity, campaign_id),
            "events": await count_campaign_rows(session, Event, campaign_id),
            "relationships": await count_campaign_rows(session, RelationshipAssertion, campaign_id),
            "thesis_versions": await count_campaign_rows(session, SceneThesis, campaign_id),
            "beliefs": int((await session.execute(select(func.count()).select_from(Belief))).scalar_one()),
            "goals": int((await session.execute(select(func.count()).select_from(CharacterGoal))).scalar_one()),
            "facts": len(await FactRepository(session).list_active(campaign_id)),
        }
        lines = [
            "# Отчёт о реалистичной автономной кампании",
            "",
            f"- Кампания: {campaign.name}",
            f"- Раундов: {turns_count}",
            f"- Строк turns: {len(all_turns)}",
            f"- Сцен: {director.transitions}",
            f"- NPC введено постепенно: {director.npcs_introduced}",
            f"- Сценарных импульсов: {director.pulses_fired}",
            f"- Ошибок генерации: {stats['generation_failures']}",
            f"- Принято дельт: {stats['accepted']}",
            f"- Отклонено дельт: {stats['rejected']}",
            f"- Среднее активных тезисов: {average_theses:.2f}",
            f"- Диапазон активных тезисов: {min(thesis_counts or [0])}..{max(thesis_counts or [0])}",
            f"- Отклонено player-outcomes: {policy.rejected_outcomes}",
            f"- Отклонено повторов: {policy.repeated_actions}",
            *(f"- {name}: {value}" for name, value in counts.items()),
            f"- Время: {(time.time() - started) / 60:.2f} минут",
            "",
            "## Поведение игрока",
            *(f"- {mode}: {policy.mode_counts[mode]}" for mode in ("action", "dialogue", "question", "plan", "decision")),
            "",
            f"- SQLite: `{database_path}`",
            f"- Лог: `{log_path}`",
            f"- JSONL: `{trace_path}`",
        ]
        report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        await session.commit()
        await session.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))

    await engine.dispose()
    print("=== REALISTIC AUTONOMOUS CAMPAIGN COMPLETED ===")


if __name__ == "__main__":
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run_realistic_simulation())
