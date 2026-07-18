"""Production entrypoint for the objective-driven autonomous campaign benchmark.

The state machine lives in ``run_realistic_simulation_v2``. This facade installs a
strict JSON control-plane, restores player policy across process restarts, preserves the
exact resumed intent and writes an authoritative report from SQLite, trace, state and
control-health evidence.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
from collections import Counter
from pathlib import Path

try:
    from . import run_realistic_simulation_v2 as runtime
    from . import simulation_quality_controls as quality
except ImportError:
    import run_realistic_simulation_v2 as runtime
    import simulation_quality_controls as quality


quality.install_quality_controls(runtime)

_FALLBACK_FINGERPRINTS: set[str] = set()
_RESUME_DECISION = None
_BasePlayerPolicy = runtime.PlayerPolicy
_BaseTraceStore = runtime.TraceStore
_BasePlayerDecision = runtime.PlayerDecision
_original_build_character_card = runtime.build_character_card
_original_generate_player_decision = runtime.generate_player_decision
_original_find_logical_pair = runtime.find_logical_pair


class RestoredPlayerPolicy(_BasePlayerPolicy):
    """Restore repetition memory and behavior counters from the unique trace."""

    def __init__(self) -> None:
        super().__init__()
        data_dir = Path(os.getenv("PDM_SIM_DATA_DIR", "./data"))
        trace_path = data_dir / "realistic_simulation_trace.jsonl"
        restored = _BaseTraceStore(trace_path)
        for turn_number in sorted(restored.records):
            player = restored.records[turn_number].get("player") or {}
            try:
                decision = _BasePlayerDecision(
                    target=str(player.get("target", "narrator")),
                    mode=str(player.get("mode", "action")),
                    intent=str(player.get("intent", "")),
                )
            except Exception:
                continue
            if decision.intent.strip():
                self.remember(decision)
            if player.get("fallback"):
                self.fallbacks += 1
                _FALLBACK_FINGERPRINTS.add(self.fingerprint(decision.intent))

    def fallback(self, *args, **kwargs):
        decision = super().fallback(*args, **kwargs)
        _FALLBACK_FINGERPRINTS.add(self.fingerprint(decision.intent))
        return decision


class TrackedTraceStore(_BaseTraceStore):
    def upsert(self, record: dict) -> None:
        player = record.get("player") or {}
        intent = str(player.get("intent", ""))
        fingerprint = _BasePlayerPolicy.fingerprint(intent)
        player["fallback"] = bool(fingerprint in _FALLBACK_FINGERPRINTS)
        record["player"] = player
        super().upsert(record)


async def tracked_build_character_card(*args, **kwargs):
    card, source = await _original_build_character_card(*args, **kwargs)
    visual_profile = dict(card.visual_profile or {})
    visual_profile["builder_source"] = source
    return card.model_copy(update={"visual_profile": visual_profile}), source


def _decision_from_trace(logical_turn: int):
    data_dir = Path(os.getenv("PDM_SIM_DATA_DIR", "./data"))
    trace = _BaseTraceStore(data_dir / "realistic_simulation_trace.jsonl")
    record = trace.records.get(logical_turn) or {}
    player = record.get("player") or {}
    if not str(player.get("intent", "")).strip():
        return None
    try:
        return _BasePlayerDecision(
            target=str(player.get("target", "narrator")),
            mode=str(player.get("mode", "action")),
            intent=str(player.get("intent", "")),
        )
    except Exception:
        return None


def _infer_mode(intent: str, target: str) -> str:
    folded = intent.casefold()
    if "спрашива" in folded or intent.rstrip().endswith("?"):
        return "question"
    if "предлага" in folded or "сопоставляю" in folded:
        return "plan"
    if "выбираю" in folded or "формулирую" in folded or "решаю" in folded:
        return "decision"
    if target != "narrator" and any(word in folded for word in ("говорю", "объясняю", "прошу")):
        return "dialogue"
    return "action"


def _decision_from_user_content(content: str):
    match = re.match(r"^\s*\[/talk\s+([^\]]+)\]\s*(.*)$", content, flags=re.DOTALL)
    if not match:
        return None
    target = match.group(1).strip()
    intent = match.group(2).strip()
    if not intent:
        return None
    return _BasePlayerDecision(
        target=target,
        mode=_infer_mode(intent, target),
        intent=intent,
    )


async def tracked_find_logical_pair(session, campaign_id, run_id, logical_turn):
    global _RESUME_DECISION
    user, assistant = await _original_find_logical_pair(
        session,
        campaign_id,
        run_id,
        logical_turn,
    )
    decision = _decision_from_trace(logical_turn)
    if decision is None and user is not None:
        decision = _decision_from_user_content(user.content)
    _RESUME_DECISION = decision
    return user, assistant


def resume_aware_player_decision(target: str, mode: str, intent: str):
    if intent.startswith("Восстановленный после сбоя") and _RESUME_DECISION is not None:
        return _RESUME_DECISION
    return _BasePlayerDecision(target=target, mode=mode, intent=intent)


def _retry_decision_from_trace():
    data_dir = Path(os.getenv("PDM_SIM_DATA_DIR", "./data"))
    state_path = data_dir / "realistic_simulation_state.json"
    trace_path = data_dir / "realistic_simulation_trace.jsonl"
    if not state_path.exists() or not trace_path.exists():
        return None
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        logical_turn = int(state["logical_turn"])
        record = _BaseTraceStore(trace_path).records.get(logical_turn)
        if not record or not record.get("generation_failed"):
            return None
        player = record.get("player") or {}
        return _BasePlayerDecision(
            target=str(player["target"]),
            mode=str(player["mode"]),
            intent=str(player["intent"]),
        )
    except Exception:
        return None


async def resumable_generate_player_decision(*args, **kwargs):
    previous = _retry_decision_from_trace()
    if previous is not None:
        return previous
    return await _original_generate_player_decision(*args, **kwargs)


def _safe_json(value, default):
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default


def _scalar(connection: sqlite3.Connection, query: str, params=()) -> int:
    row = connection.execute(query, params).fetchone()
    return int(row[0] or 0) if row else 0


def _campaign_report(database_path: Path, data_dir: Path) -> list[str]:
    state_path = data_dir / "realistic_simulation_state.json"
    trace_path = data_dir / "realistic_simulation_trace.jsonl"
    health_path = data_dir / "realistic_simulation_health.json"
    state = _safe_json(
        state_path.read_text(encoding="utf-8") if state_path.exists() else None,
        {},
    )
    health = _safe_json(
        health_path.read_text(encoding="utf-8") if health_path.exists() else None,
        quality.health_snapshot(),
    )
    campaign_id = state.get("campaign_id")
    if not database_path.exists() or not campaign_id:
        return ["- Аудит БД недоступен: нет database или campaign_id."]

    trace = _BaseTraceStore(trace_path)
    records = list(trace.records.values())
    modes = Counter(
        str((record.get("player") or {}).get("mode", "unknown"))
        for record in records
    )
    unique_intents = {
        _BasePlayerPolicy.fingerprint(str((record.get("player") or {}).get("intent", "")))
        for record in records
        if str((record.get("player") or {}).get("intent", "")).strip()
    }
    trace_failures = sum(bool(record.get("generation_failed")) for record in records)
    fallbacks = sum(bool((record.get("player") or {}).get("fallback")) for record in records)

    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA foreign_keys=ON")
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    foreign_key_violations = len(connection.execute("PRAGMA foreign_key_check").fetchall())

    turns = _scalar(connection, "SELECT COUNT(*) FROM turns WHERE campaign_id=?", (campaign_id,))
    active_turns = _scalar(
        connection,
        "SELECT COUNT(*) FROM turns WHERE campaign_id=? AND status='active'",
        (campaign_id,),
    )
    failed_users = _scalar(
        connection,
        "SELECT COUNT(*) FROM turns WHERE campaign_id=? AND role='user' AND status='failed'",
        (campaign_id,),
    )
    alternative_assistants = _scalar(
        connection,
        "SELECT COUNT(*) FROM turns WHERE campaign_id=? AND role='assistant' AND status='alternative'",
        (campaign_id,),
    )
    interrupted_assistants = _scalar(
        connection,
        "SELECT COUNT(*) FROM turns WHERE campaign_id=? AND role='assistant' AND content LIKE '%[generation interrupted]%'",
        (campaign_id,),
    )
    scenes = _scalar(connection, "SELECT COUNT(*) FROM scenes WHERE campaign_id=?", (campaign_id,))
    completed_scenes = _scalar(
        connection,
        "SELECT COUNT(*) FROM scenes WHERE campaign_id=? AND status='completed'",
        (campaign_id,),
    )
    characters = _scalar(
        connection,
        "SELECT COUNT(*) FROM entities WHERE campaign_id=? AND entity_type='character'",
        (campaign_id,),
    )
    entities = _scalar(connection, "SELECT COUNT(*) FROM entities WHERE campaign_id=?", (campaign_id,))
    events = _scalar(connection, "SELECT COUNT(*) FROM events WHERE campaign_id=?", (campaign_id,))
    pulses = _scalar(
        connection,
        "SELECT COUNT(*) FROM events WHERE campaign_id=? AND event_type='scenario_pulse'",
        (campaign_id,),
    )
    outcomes = _scalar(
        connection,
        "SELECT COUNT(*) FROM events WHERE campaign_id=? AND event_type='scene_outcome'",
        (campaign_id,),
    )
    facts = _scalar(
        connection,
        "SELECT COUNT(*) FROM facts WHERE campaign_id=? AND is_current=1",
        (campaign_id,),
    )
    relationships = _scalar(
        connection,
        "SELECT COUNT(*) FROM relationship_assertions WHERE campaign_id=? AND is_current=1",
        (campaign_id,),
    )
    beliefs = _scalar(
        connection,
        """SELECT COUNT(*) FROM beliefs b
           JOIN entities e ON e.id=b.character_id
           WHERE e.campaign_id=?""",
        (campaign_id,),
    )
    sourced_beliefs = _scalar(
        connection,
        """SELECT COUNT(*) FROM beliefs b
           JOIN entities e ON e.id=b.character_id
           WHERE e.campaign_id=? AND b.source_turn_id IS NOT NULL""",
        (campaign_id,),
    )
    goals = _scalar(
        connection,
        """SELECT COUNT(*) FROM character_goals g
           JOIN entities e ON e.id=g.character_id
           WHERE e.campaign_id=?""",
        (campaign_id,),
    )
    thesis_versions = _scalar(
        connection,
        """SELECT COUNT(*) FROM scene_theses st
           JOIN scenes s ON s.id=st.scene_id
           WHERE s.campaign_id=?""",
        (campaign_id,),
    )
    active_theses = _scalar(
        connection,
        """SELECT COUNT(*) FROM scene_theses st
           JOIN scenes s ON s.id=st.scene_id
           WHERE s.campaign_id=? AND st.status='active'""",
        (campaign_id,),
    )
    completed_scene_active = _scalar(
        connection,
        """SELECT COUNT(*) FROM scene_theses st
           JOIN scenes s ON s.id=st.scene_id
           WHERE s.campaign_id=? AND s.status='completed' AND st.status='active'""",
        (campaign_id,),
    )

    proposal_rows = connection.execute(
        """SELECT pc.status, COUNT(*)
           FROM proposed_changes pc
           JOIN turns t ON t.id=pc.turn_id
           WHERE t.campaign_id=?
           GROUP BY pc.status""",
        (campaign_id,),
    ).fetchall()
    proposals = {status: int(count) for status, count in proposal_rows}

    builder_sources = Counter()
    for (visual_profile,) in connection.execute(
        """SELECT c.visual_profile FROM characters c
           JOIN entities e ON e.id=c.entity_id
           WHERE e.campaign_id=? AND e.canonical_name!='Eldon'""",
        (campaign_id,),
    ):
        profile = _safe_json(visual_profile, {})
        source = profile.get("builder_source")
        if not source and profile.get("fallback"):
            source = "fallback"
        builder_sources[source or "legacy_unknown"] += 1

    actor_contexts = 0
    actor_contexts_missing_current = 0
    for acting_character_id, snapshot_raw in connection.execute(
        """SELECT acting_character_id, context_snapshot FROM turns
           WHERE campaign_id=? AND role='assistant' AND acting_character_id IS NOT NULL""",
        (campaign_id,),
    ):
        snapshot = _safe_json(snapshot_raw, {})
        actor_contexts += 1
        if not snapshot.get("current_user_reserved"):
            actor_contexts_missing_current += 1

    connection.close()

    control_stats = health.get("control_stats") or {}
    control_failures = list(health.get("control_failures") or [])
    total_generation_failures = max(trace_failures, failed_users, alternative_assistants)
    invalid_reasons: list[str] = []
    if control_failures:
        invalid_reasons.append("control-plane failures")
    if quality.benchmark_mode() == "quality" and fallbacks:
        invalid_reasons.append("player fallback used in quality mode")
    if quality.benchmark_mode() == "quality" and builder_sources["fallback"]:
        invalid_reasons.append("Character Builder fallback used in quality mode")
    if total_generation_failures:
        invalid_reasons.append("generation failures or partial alternatives exist")
    if actor_contexts_missing_current:
        invalid_reasons.append("actor context omitted current player message")
    if completed_scene_active:
        invalid_reasons.append("completed scenes retain active theses")
    if quality.benchmark_mode() == "quality" and not control_stats.get("evaluator_success"):
        invalid_reasons.append("Evaluator never completed successfully")
    if quality.benchmark_mode() == "quality" and not control_stats.get("scribe_success"):
        invalid_reasons.append("Memory Scribe never completed successfully")
    benchmark_valid = not invalid_reasons

    return [
        f"- Benchmark mode: `{quality.benchmark_mode()}`",
        f"- Benchmark valid: **{benchmark_valid}**",
        f"- Invalid reasons: {'; '.join(invalid_reasons) if invalid_reasons else 'none'}",
        f"- Run ID: `{state.get('run_id', 'unknown')}`",
        f"- Уникальных логических ходов: {len(records)}",
        f"- Следующий логический ход: {state.get('logical_turn', 1)}",
        f"- Фаз завершено: {state.get('phase_index', 0)}/10",
        f"- Кампания завершена: {bool(state.get('completed'))}",
        f"- Ошибок в trace/failed users/alternative assistants: {trace_failures}/{failed_users}/{alternative_assistants}",
        f"- Interrupted assistant texts: {interrupted_assistants}",
        f"- Уникальных player intents: {len(unique_intents)}",
        f"- Player fallbacks: {fallbacks}",
        "- Режимы игрока: " + ", ".join(f"{name}={modes[name]}" for name in sorted(modes)),
        f"- Строк turns: {turns} (active={active_turns})",
        f"- Actor contexts/current-message omissions: {actor_contexts}/{actor_contexts_missing_current}",
        f"- Сцен: {scenes} (completed={completed_scenes})",
        f"- Персонажей: {characters} (NPC={max(0, characters - 1)})",
        "- Character Builder model/repair/fallback/legacy: "
        f"{builder_sources['model']}/{builder_sources['repair']}/{builder_sources['fallback']}/{builder_sources['legacy_unknown']}",
        f"- Control stats: `{json.dumps(control_stats, ensure_ascii=False, sort_keys=True)}`",
        f"- Control failures: {len(control_failures)}",
        f"- Entities: {entities}",
        f"- Events: {events} (confirmed pulses={pulses}, scene outcomes={outcomes})",
        f"- Facts: {facts}",
        f"- Beliefs: {beliefs} (sourced from turns={sourced_beliefs})",
        f"- Goals: {goals}",
        f"- Relationships: {relationships}",
        f"- Thesis versions: {thesis_versions}",
        f"- Active theses: {active_theses}",
        f"- Active theses in completed scenes: {completed_scene_active}",
        f"- Proposals accepted/rejected/invalid/proposed: {proposals.get('accepted', 0)}/{proposals.get('rejected', 0)}/{proposals.get('invalid', 0)}/{proposals.get('proposed', 0)}",
        f"- SQLite integrity: {integrity}",
        f"- Foreign-key violations: {foreign_key_violations}",
    ]


def write_authoritative_report() -> None:
    data_dir = Path(os.getenv("PDM_SIM_DATA_DIR", "./data"))
    database_path = Path(os.getenv("PDM_SIM_DB", str(data_dir / "realistic_simulation.db")))
    report_path = data_dir / "realistic_simulation_report.md"
    lines = [
        "# Отчёт о реалистичной автономной кампании",
        "",
        "Отчёт пересчитан из SQLite, idempotent JSONL, state и control-health файлов.",
        "In-memory счётчики не используются как источник истины.",
        "",
        *_campaign_report(database_path, data_dir),
        "",
        f"- SQLite: `{database_path}`",
        f"- State: `{data_dir / 'realistic_simulation_state.json'}`",
        f"- Health: `{data_dir / 'realistic_simulation_health.json'}`",
        f"- Лог: `{data_dir / 'realistic_simulation_play.log'}`",
        f"- JSONL: `{data_dir / 'realistic_simulation_trace.jsonl'}`",
    ]
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def run_realistic_simulation() -> None:
    runtime.PlayerPolicy = RestoredPlayerPolicy
    runtime.TraceStore = TrackedTraceStore
    runtime.PlayerDecision = resume_aware_player_decision
    runtime.find_logical_pair = tracked_find_logical_pair
    runtime.build_character_card = tracked_build_character_card
    runtime.generate_player_decision = resumable_generate_player_decision
    try:
        await runtime.run_realistic_simulation_v2()
    finally:
        quality._write_health()
        write_authoritative_report()


if __name__ == "__main__":
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run_realistic_simulation())
