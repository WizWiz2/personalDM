from __future__ import annotations

from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected one match, found {count}: {old[:120]!r}")
    target.write_text(text.replace(old, new), encoding="utf-8")


# ---------------------------------------------------------------------------
# Durable fact scope: local observations must not leak into every later scene.
# ---------------------------------------------------------------------------
replace_once(
    "src/backend/app/db/tables.py",
    '''    visibility: Mapped[str] = mapped_column(String(50), default="dm", nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
''',
    '''    visibility: Mapped[str] = mapped_column(String(50), default="dm", nullable=False)
    scope: Mapped[str] = mapped_column(String(50), default="campaign", nullable=False)
    scene_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("scenes.id", ondelete="CASCADE"), nullable=True
    )
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
''',
)
replace_once(
    "src/backend/app/db/tables.py",
    '''    campaign = relationship("Campaign", back_populates="facts")
    beliefs = relationship("Belief", back_populates="fact", cascade="all, delete-orphan")
''',
    '''    campaign = relationship("Campaign", back_populates="facts")
    beliefs = relationship("Belief", back_populates="fact", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint(
            "(scope = 'campaign' AND scene_id IS NULL) OR "
            "(scope = 'scene' AND scene_id IS NOT NULL)",
            name="ck_fact_scope_scene",
        ),
    )
''',
)

replace_once(
    "src/backend/app/services/canon_applier.py",
    '''                    visibility=payload.get("visibility", "dm"),
                    source_turn_id=source_turn_id,
''',
    '''                    visibility=payload.get("visibility", "dm"),
                    source_turn_id=source_turn_id,
                    scope=payload.get("scope", "campaign"),
                    scene_id=(
                        UUID(payload["scene_id"])
                        if payload.get("scene_id")
                        else None
                    ),
''',
)

replace_once(
    "src/backend/app/services/context_compiler.py",
    '''        facts = await self._fact_repo.list_active(
            campaign_id,
            visibility="public" if actor_mode else None,
        )
''',
    '''        facts = await self._fact_repo.list_active(
            campaign_id,
            visibility="public" if actor_mode else None,
            scene_id=scene_id,
        )
''',
)

# Receipt may show knowledge, but knowledge-only chatter no longer suppresses stagnation.
replace_once(
    "src/backend/app/services/context_compiler.py",
    '''        by_turn: dict[str, list[str]] = {turn_id: [] for turn_id in recent_turn_ids}
        for proposal in proposals:
''',
    '''        by_turn: dict[str, list[str]] = {turn_id: [] for turn_id in recent_turn_ids}
        progress_by_turn: dict[str, bool] = {
            turn_id: False for turn_id in recent_turn_ids
        }
        for proposal in proposals:
''',
)
replace_once(
    "src/backend/app/services/context_compiler.py",
    '''            if summary and summary not in by_turn[proposal.turn_id]:
                by_turn[proposal.turn_id].append(summary)
''',
    '''            if summary and summary not in by_turn[proposal.turn_id]:
                by_turn[proposal.turn_id].append(summary)
            if proposal.change_type in {
                "fact",
                "event",
                "relationship",
                "movement",
                "item_transfer",
            }:
                progress_by_turn[proposal.turn_id] = True
''',
)
replace_once(
    "src/backend/app/services/context_compiler.py",
    '''        stagnant = len(watched) == stagnation_window and all(
            not by_turn[turn_id] for turn_id in watched
        )
''',
    '''        stagnant = len(watched) == stagnation_window and all(
            not progress_by_turn[turn_id] for turn_id in watched
        )
''',
)

# Memory Scribe defaults observations to the current scene; it must opt into campaign scope.
replace_once(
    "src/backend/app/services/memory_scribe.py",
    '''        current_facts = await self._fact_repo.list_active(campaign_id)
''',
    '''        current_facts = await self._fact_repo.list_active(
            campaign_id,
            scene_id=scene_id,
        )
''',
)
replace_once(
    "src/backend/app/services/memory_scribe.py",
    '''- fact: {{"subject":"устойчивый субъект","predicate":"стабильная связь","object_value":"значение или null","truth_status":"true|false|disputed","visibility":"dm|public"}}
''',
    '''- fact: {{"subject":"устойчивый субъект","predicate":"стабильная связь","object_value":"значение или null","truth_status":"true|false|disputed","visibility":"dm|public","scope":"scene|campaign"}}
- scope=scene для следов, положения, состояния двери, локальной находки и любых наблюдений, истинных только здесь.
- scope=campaign только для личности, происхождения, владения, глобального лора или устойчивого состояния, которое должно пережить смену сцены.
''',
)
replace_once(
    "src/backend/app/services/memory_scribe.py",
    '''            scene_participant_ids=scene_participant_ids,
        )
''',
    '''            scene_participant_ids=scene_participant_ids,
            scene_id=scene_id,
        )
''',
)
replace_once(
    "src/backend/app/services/memory_scribe.py",
    '''        authoritative_text: str = "",
    ) -> list[ProposedChangeCreate]:
''',
    '''        authoritative_text: str = "",
        scene_id: UUID | None = None,
    ) -> list[ProposedChangeCreate]:
''',
)
replace_once(
    "src/backend/app/services/memory_scribe.py",
    '''            player_character_id,
            scene_participant_ids,
        )
''',
    '''            player_character_id,
            scene_participant_ids,
            scene_id,
        )
''',
)
replace_once(
    "src/backend/app/services/memory_scribe.py",
    '''        scene_participant_ids: list[str],
    ) -> list[ProposedChangeCreate]:
''',
    '''        scene_participant_ids: list[str],
        scene_id: UUID | None = None,
    ) -> list[ProposedChangeCreate]:
''',
)
replace_once(
    "src/backend/app/services/memory_scribe.py",
    '''                player_character_id,
                scene_participant_ids,
            )
''',
    '''                player_character_id,
                scene_participant_ids,
                scene_id,
            )
''',
)
replace_once(
    "src/backend/app/services/memory_scribe.py",
    '''        scene_participant_ids: list[str],
    ) -> dict | None:
''',
    '''        scene_participant_ids: list[str],
        scene_id: UUID | None = None,
    ) -> dict | None:
''',
)
replace_once(
    "src/backend/app/services/memory_scribe.py",
    '''            resolved["operation"] = operation
            resolved["cardinality"] = cardinality
''',
    '''            resolved["operation"] = operation
            resolved["cardinality"] = cardinality
            scope = str(resolved.get("scope") or "scene").casefold()
            if scope not in {"campaign", "scene"}:
                scope = "scene"
            if scope == "scene" and scene_id is not None:
                resolved["scope"] = "scene"
                resolved["scene_id"] = str(scene_id)
            else:
                resolved["scope"] = "campaign"
                resolved.pop("scene_id", None)
''',
)

# ---------------------------------------------------------------------------
# Scenario dataclasses gain formal completion criteria and bounded scene lengths.
# ---------------------------------------------------------------------------
replace_once(
    "src/backend/tests/simulation_scenario.py",
    '''@dataclass(frozen=True)
class ScenarioPhase:
''',
    '''@dataclass(frozen=True)
class ObjectiveCriterion:
    key: str
    description: str
    allowed_change_types: tuple[str, ...]


@dataclass(frozen=True)
class ScenarioPhase:
''',
)
replace_once(
    "src/backend/tests/simulation_scenario.py",
    '''    pulses: tuple[ScenarioPulse, ...]
    director_note: str
''',
    '''    pulses: tuple[ScenarioPulse, ...]
    director_note: str
    completion_criteria: tuple[ObjectiveCriterion, ...] = ()
    min_turns: int = 6
    max_turns: int = 18
    arc_index: int = 0
''',
)

# ---------------------------------------------------------------------------
# Runtime: Alembic DB, generated arcs, LLM player routing and criterion ledger.
# ---------------------------------------------------------------------------
replace_once(
    "src/backend/tests/run_realistic_simulation_v2.py",
    '''from app.db.engine import Base
''',
    '''''',
)
replace_once(
    "src/backend/tests/run_realistic_simulation_v2.py",
    '''try:
    from .simulation_scenario import NPCS, PHASES, NpcConcept, ScenarioPhase
except ImportError:
    from simulation_scenario import NPCS, PHASES, NpcConcept, ScenarioPhase
''',
    '''try:
    from .simulation_database import upgrade_simulation_database
    from .simulation_dynamic_campaign import (
        CampaignCatalog,
        catalog_summary,
        ensure_phase_available,
    )
    from .simulation_scenario import NPCS, PHASES, NpcConcept, ScenarioPhase
except ImportError:
    from simulation_database import upgrade_simulation_database
    from simulation_dynamic_campaign import (
        CampaignCatalog,
        catalog_summary,
        ensure_phase_available,
    )
    from simulation_scenario import NPCS, PHASES, NpcConcept, ScenarioPhase
''',
)
replace_once(
    "src/backend/tests/run_realistic_simulation_v2.py",
    '''class ObjectiveEvaluation(BaseModel):
    status: Literal["progressing", "resolved", "failed", "blocked"] = "progressing"
    evidence: str = ""
    outcome_summary: str | None = None
    confirmed_pulses: list[int] = Field(default_factory=list)
''',
    '''class ObjectiveEvaluation(BaseModel):
    status: Literal["progressing", "resolved", "failed", "blocked"] = "progressing"
    evidence: str = ""
    outcome_summary: str | None = None
    confirmed_pulses: list[int] = Field(default_factory=list)
    criteria_met: list[str] = Field(default_factory=list)
    criterion_evidence: dict[str, str] = Field(default_factory=dict)
''',
)
replace_once(
    "src/backend/tests/run_realistic_simulation_v2.py",
    '''    confirmed_pulses: set[int] = field(default_factory=set)
''',
    '''    confirmed_pulses: set[int] = field(default_factory=set)
    criteria_met: set[str] = field(default_factory=set)
    durable_changes: list[str] = field(default_factory=list)
''',
)
replace_once(
    "src/backend/tests/run_realistic_simulation_v2.py",
    '''    confirmed_pulses: list[int] = field(default_factory=list)
    consecutive_failures: int = 0
    completed: bool = False
''',
    '''    confirmed_pulses: list[int] = field(default_factory=list)
    criteria_met: list[str] = field(default_factory=list)
    durable_changes: list[str] = field(default_factory=list)
    player_journal: list[str] = field(default_factory=list)
    consecutive_failures: int = 0
    completed: bool = False
''',
)
replace_once(
    "src/backend/tests/run_realistic_simulation_v2.py",
    '''async def run_realistic_simulation_v2() -> None:
    data_dir = Path(os.getenv("PDM_SIM_DATA_DIR", "./data"))
''',
    '''async def run_realistic_simulation_v2() -> None:
    global NPCS, PHASES
    data_dir = Path(os.getenv("PDM_SIM_DATA_DIR", "./data"))
''',
)
replace_once(
    "src/backend/tests/run_realistic_simulation_v2.py",
    '''    state_path = data_dir / "realistic_simulation_state.json"
''',
    '''    state_path = data_dir / "realistic_simulation_state.json"
    scenario_path = data_dir / "realistic_simulation_scenario.json"
''',
)
replace_once(
    "src/backend/tests/run_realistic_simulation_v2.py",
    '''            state_path,
        ):
''',
    '''            state_path,
            scenario_path,
        ):
''',
)
replace_once(
    "src/backend/tests/run_realistic_simulation_v2.py",
    '''    phase_budget = max(10, turns_limit // len(PHASES))
    minimum_phase_turns = max(4, phase_budget // 3)
    hard_phase_limit = phase_budget + 4
''',
    '''''',
)
replace_once(
    "src/backend/tests/run_realistic_simulation_v2.py",
    '''    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
''',
    '''    alembic_revision = upgrade_simulation_database(database_path)
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
''',
)
replace_once(
    "src/backend/tests/run_realistic_simulation_v2.py",
    '''        role_router = RoleModelRouter(config_repo)
        builder_selection = await role_router.resolve(
''',
    '''        role_router = RoleModelRouter(config_repo)
        provider = LLMProvider()
        builder_selection = await role_router.resolve(
''',
)
replace_once(
    "src/backend/tests/run_realistic_simulation_v2.py",
    '''        evaluator_selection = await role_router.resolve(
            campaign_id,
            ModelRole.EVALUATOR,
            config,
        )
        if builder_selection is None or evaluator_selection is None:
            raise RuntimeError("Role model routing requires a configured campaign provider")
''',
    '''        evaluator_selection = await role_router.resolve(
            campaign_id,
            ModelRole.EVALUATOR,
            config,
        )
        player_selection = await role_router.resolve(
            campaign_id,
            ModelRole.PLAYER,
            config,
        )
        scenario_selection = await role_router.resolve(
            campaign_id,
            ModelRole.SCENARIO_BUILDER,
            config,
        )
        if any(
            selection is None
            for selection in (
                builder_selection,
                evaluator_selection,
                player_selection,
                scenario_selection,
            )
        ):
            raise RuntimeError("Role model routing requires a configured campaign provider")
''',
)
replace_once(
    "src/backend/tests/run_realistic_simulation_v2.py",
    '''        provider = LLMProvider()
        stats: Counter = Counter()
        director = ScenarioDirector(
''',
    '''        catalog = await ensure_phase_available(
            path=scenario_path,
            reset=should_reset,
            phase_index=state.phase_index,
            provider=provider,
            router=role_router,
            selection=scenario_selection,
            previous_outcomes=state.player_journal,
        )
        NPCS = catalog.runtime_npcs()
        PHASES = catalog.runtime_phases()

        stats: Counter = Counter()
        director = ScenarioDirector(
''',
)
replace_once(
    "src/backend/tests/run_realistic_simulation_v2.py",
    '''        while (
            state.logical_turn <= turns_limit
            and state.phase_index < len(PHASES)
            and not state.completed
        ):
            runtime = await director.enter_phase(state.phase_index, state)
''',
    '''        while state.logical_turn <= turns_limit and not state.completed:
            if state.phase_index >= len(PHASES):
                catalog = await ensure_phase_available(
                    path=scenario_path,
                    reset=False,
                    phase_index=state.phase_index,
                    provider=provider,
                    router=role_router,
                    selection=scenario_selection,
                    previous_outcomes=state.player_journal,
                )
                NPCS = catalog.runtime_npcs()
                PHASES = catalog.runtime_phases()
                await director.restore_characters()

            runtime = await director.enter_phase(state.phase_index, state)
''',
)
replace_once(
    "src/backend/tests/run_realistic_simulation_v2.py",
    '''            runtime.confirmed_pulses = set(state.confirmed_pulses)
            await director.inject_due_pulses(runtime, hard_phase_limit)
''',
    '''            runtime.confirmed_pulses = set(state.confirmed_pulses)
            runtime.criteria_met = set(state.criteria_met)
            runtime.durable_changes = list(state.durable_changes)
            minimum_phase_turns = max(4, int(runtime.phase.min_turns))
            hard_phase_limit = max(
                minimum_phase_turns + 4,
                int(runtime.phase.max_turns),
            )
            await director.inject_due_pulses(runtime, hard_phase_limit)
''',
)
replace_once(
    "src/backend/tests/run_realistic_simulation_v2.py",
    '''                    provider,
                    config,
                    api_key,
''',
    '''                    provider,
                    player_selection.config,
                    player_selection.api_key,
''',
)
replace_once(
    "src/backend/tests/run_realistic_simulation_v2.py",
    '''                accepted, rejected = await resolve_turn_proposals(
                    session,
                    assistant_turn_id,
                )
''',
    '''                accepted, rejected = await resolve_turn_proposals(
                    session,
                    assistant_turn_id,
                )
                for change in accepted:
                    if change not in runtime.durable_changes:
                        runtime.durable_changes.append(change)
                runtime.durable_changes = runtime.durable_changes[-80:]
''',
)
replace_once(
    "src/backend/tests/run_realistic_simulation_v2.py",
    '''            state.injected_pulses = sorted(runtime.injected_pulses)
            state.confirmed_pulses = sorted(runtime.confirmed_pulses)

            if phase_finished:
''',
    '''            state.injected_pulses = sorted(runtime.injected_pulses)
            state.confirmed_pulses = sorted(runtime.confirmed_pulses)
            state.criteria_met = sorted(runtime.criteria_met)
            state.durable_changes = list(runtime.durable_changes)

            if phase_finished:
''',
)
replace_once(
    "src/backend/tests/run_realistic_simulation_v2.py",
    '''                state.phase_index += 1
                state.phase_turn = 0
                state.injected_pulses = []
                state.confirmed_pulses = []
                director.current = None
                if state.phase_index >= len(PHASES):
                    state.completed = True
''',
    '''                state.player_journal.append(
                    evaluation.outcome_summary
                    or evaluation.evidence
                    or f"Сцена {runtime.phase.title} завершена со статусом {evaluation.status}."
                )
                state.player_journal = state.player_journal[-24:]
                state.phase_index += 1
                state.phase_turn = 0
                state.injected_pulses = []
                state.confirmed_pulses = []
                state.criteria_met = []
                state.durable_changes = []
                director.current = None
''',
)
replace_once(
    "src/backend/tests/run_realistic_simulation_v2.py",
    '''        all_turns = await turns.get_history(campaign_id, limit=turns_limit * 4, active_only=False)
''',
    '''        if state.logical_turn > turns_limit:
            state.completed = True
            state.save(state_path)

        all_turns = await turns.get_history(campaign_id, limit=turns_limit * 4, active_only=False)
''',
)
replace_once(
    "src/backend/tests/run_realistic_simulation_v2.py",
    '''            f"- Run ID: `{state.run_id}`",
''',
    '''            f"- Run ID: `{state.run_id}`",
            f"- Alembic revision: `{alembic_revision}`",
            f"- Generated scenario: `{json.dumps(catalog_summary(catalog), ensure_ascii=False)}`",
''',
)

# ---------------------------------------------------------------------------
# Quality controls: LLM player by default and strict criterion-backed closure.
# ---------------------------------------------------------------------------
quality_path = Path("src/backend/tests/simulation_quality_controls.py")
quality_text = quality_path.read_text(encoding="utf-8")
insert_after = '''def evaluator_history_without_duplicate(
    recent_history,
    assistant_content: str,
    *,
    limit: int = 6,
):
'''
if insert_after not in quality_text:
    raise SystemExit("quality evaluator helper anchor missing")
helper = '''def _normalized(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def enforce_objective_contract(evaluation, phase_runtime):
    criteria = {item.key: item for item in phase_runtime.phase.completion_criteria}
    if not criteria:
        return evaluation

    ledger = list(phase_runtime.durable_changes)
    supported: set[str] = set(phase_runtime.criteria_met)
    accepted_evidence: dict[str, str] = {}
    for key in evaluation.criteria_met:
        criterion = criteria.get(key)
        evidence = str(evaluation.criterion_evidence.get(key) or "").strip()
        if criterion is None or not evidence:
            continue
        evidence_norm = _normalized(evidence)
        for change in ledger:
            change_type = change.split(":", 1)[0].strip()
            if change_type not in criterion.allowed_change_types:
                continue
            change_norm = _normalized(change)
            if evidence_norm in change_norm or change_norm in evidence_norm:
                supported.add(key)
                accepted_evidence[key] = change
                break

    phase_runtime.criteria_met = supported
    evaluation.criteria_met = sorted(supported)
    evaluation.criterion_evidence = {
        key: accepted_evidence.get(key, evaluation.criterion_evidence.get(key, ""))
        for key in sorted(supported)
    }
    missing = sorted(set(criteria) - supported)
    if missing:
        if evaluation.status == "resolved":
            evaluation.status = "progressing"
        evaluation.evidence = (
            f"Не подтверждены критерии: {', '.join(missing)}. "
            + (evaluation.evidence or "")
        ).strip()
    elif evaluation.status not in {"failed", "blocked"}:
        evaluation.status = "resolved"
        if not evaluation.outcome_summary:
            evaluation.outcome_summary = "Все формальные критерии сцены подтверждены каноном."
    return evaluation


'''
quality_text = quality_text.replace(insert_after, helper + insert_after, 1)
quality_path.write_text(quality_text, encoding="utf-8")

replace_once(
    "src/backend/tests/simulation_quality_controls.py",
    '''        if settings.SIM_PLAYER_MODE.casefold() == "deterministic":
''',
    '''        if settings.SIM_PLAYER_MODE.casefold() == "deterministic":
''',
)
# The branch remains available for explicit CI fixtures; default setting is now llm.
replace_once(
    "src/backend/tests/simulation_quality_controls.py",
    '''ЦЕЛЬ: {phase_runtime.phase.objective}
ХОДОВ В СЦЕНЕ: {phase_runtime.phase_turn}
АКТИВНЫЕ ТЕЗИСЫ: {' | '.join(active_theses)}
''',
    '''ЦЕЛЬ: {phase_runtime.phase.objective}
ХОДОВ В СЦЕНЕ: {phase_runtime.phase_turn}
КРИТЕРИИ ЗАВЕРШЕНИЯ:
{chr(10).join(f"- {item.key}: {item.description}; допустимые изменения: {', '.join(item.allowed_change_types)}" for item in phase_runtime.phase.completion_criteria) or '- legacy phase without formal criteria'}
УЖЕ ПОДТВЕРЖДЕНЫ: {', '.join(sorted(phase_runtime.criteria_met)) or 'нет'}
ПРИНЯТЫЕ DURABLE CHANGES ЭТОЙ СЦЕНЫ:
{chr(10).join(phase_runtime.durable_changes[-50:]) or '- нет'}
АКТИВНЫЕ ТЕЗИСЫ: {' | '.join(active_theses)}
''',
)
replace_once(
    "src/backend/tests/simulation_quality_controls.py",
    '''Resolved только если цель действительно достигнута. Не считай план или тезис событием."""
''',
    '''Для criteria_met верни только ключи критериев, подтверждённых строкой из DURABLE CHANGES.
В criterion_evidence скопируй точную строку durable change для каждого ключа.
Resolved только если подтверждены все критерии. Не считай план, тезис или красивое наблюдение событием."""
''',
)
replace_once(
    "src/backend/tests/simulation_quality_controls.py",
    '''        evaluation.confirmed_pulses = [
            index
            for index in evaluation.confirmed_pulses
            if index in pending_indexes
        ]
''',
    '''        evaluation.confirmed_pulses = [
            index
            for index in evaluation.confirmed_pulses
            if index in pending_indexes
        ]
        evaluation = enforce_objective_contract(evaluation, phase_runtime)
''',
)

# Base evaluator gets the same schema hints; quality mode still supplies strict validation.
replace_once(
    "src/backend/tests/run_realistic_simulation_v2.py",
    '''ЦЕЛЬ: {runtime.phase.objective}
ХОДОВ В СЦЕНЕ: {runtime.phase_turn}
АКТИВНЫЕ ТЕЗИСЫ: {' | '.join(active_theses)}
''',
    '''ЦЕЛЬ: {runtime.phase.objective}
ХОДОВ В СЦЕНЕ: {runtime.phase_turn}
КРИТЕРИИ: {chr(10).join(f"- {item.key}: {item.description}" for item in runtime.phase.completion_criteria) or '- нет'}
DURABLE CHANGES: {chr(10).join(runtime.durable_changes[-50:]) or '- нет'}
АКТИВНЫЕ ТЕЗИСЫ: {' | '.join(active_theses)}
''',
)

# Report uses dynamic phase count and records migration/scenario files.
replace_once(
    "src/backend/tests/run_realistic_simulation.py",
    '''        f"- Фаз завершено: {state.get('phase_index', 0)}/10",
''',
    '''        f"- Фаз завершено: {state.get('phase_index', 0)} (динамический каталог)",
''',
)
replace_once(
    "src/backend/tests/run_realistic_simulation.py",
    '''        f"- State: `{data_dir / 'realistic_simulation_state.json'}`",
''',
    '''        f"- State: `{data_dir / 'realistic_simulation_state.json'}`",
        f"- Scenario: `{data_dir / 'realistic_simulation_scenario.json'}`",
''',
)

print("generative simulation transform applied")
