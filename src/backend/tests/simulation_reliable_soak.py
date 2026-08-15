from __future__ import annotations

import json
import os
import re
from collections import Counter
from typing import Any

from sqlalchemy import select

from app.db.narration_validation_table import NarrationValidationRun


SESSION_ZERO_PATTERNS = (
    re.compile(r"\bнулев(?:ая|ой|ую|ой)\s+сесси", re.IGNORECASE),
    re.compile(r"\bsession[ -]?zero\b", re.IGNORECASE),
    re.compile(r"я\s+уже\s+вс[её]\s+рассказ", re.IGNORECASE),
    re.compile(r"начина(?:й|йте|ем)\s+игр", re.IGNORECASE),
    re.compile(r"можно\s+начинать\s+игр", re.IGNORECASE),
)

TECHNICAL_PUBLICATION_PATTERNS = (
    ("authority_marker", re.compile(r"TYPED TURN AUTHORITY|EXECUTED ACTION SEQUENCE", re.IGNORECASE)),
    ("validator_jargon", re.compile(r"\bvalidator\b|validation[_ ]status|repair_required", re.IGNORECASE)),
    ("sequence_jargon", re.compile(r"\b(?:BLOCKED|SKIPPED|COMPLETED)\b", re.IGNORECASE)),
    ("route_diagnostic", re.compile(r"Destination route|available exit|target_location_id|source_location_id", re.IGNORECASE)),
    ("generation_meta", re.compile(r"Generation failed|provider returned|control[- ]plane", re.IGNORECASE)),
    ("legacy_blocked_stub", re.compile(r"Действие не выполнено\s*:", re.IGNORECASE)),
    ("legacy_no_result_stub", re.compile(r"Попытка пока не приводит к подтвержд[её]нному результату", re.IGNORECASE)),
    ("next_input_meta", re.compile(r"жд[её]т дальнейших слов игрока|следующ(?:ий|его) ввод", re.IGNORECASE)),
    (
        "uuid",
        re.compile(
            r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
            r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
            re.IGNORECASE,
        ),
    ),
)

# This corpus is intentionally deterministic. It is not a second DM and never reads
# session-zero prose to decide what the player does. Dynamic interpolation is limited to
# the current public objective and an active NPC name chosen deterministically.
SCRIPTED_TURNS: tuple[tuple[str, str, str], ...] = (
    ("action", "narrator", "Осматриваю ближайшее окружение: людей, предметы, свет и доступные выходы. Ничего не трогаю, пока не пойму, что здесь действительно находится."),
    ("question", "npc", "Спрашиваю {npc}: «Что ты лично видел здесь совсем недавно? Отдели собственное наблюдение от слухов»."),
    ("action", "narrator", "Проверяю самый очевидный безопасный путь из текущего места и смотрю, куда он реально ведёт, не объявляя, что уже добрался до цели."),
    ("dialogue", "npc", "Говорю {npc}: «Я хочу сначала проверить факты. Если заметишь, что я упускаю конкретную деталь, скажи прямо»."),
    ("action", "narrator", "Ищу физические следы недавних действий: грязь, царапины, переставленные вещи, свежие следы обуви или открытые крепления."),
    ("plan", "npc", "Предлагаю {npc} простой порядок: сначала сверить видимые следы, потом задать один уточняющий вопрос и только после этого выбирать рискованный шаг."),
    ("action", "narrator", "Подхожу к ближайшей явно доступной границе пространства и проверяю её обычным способом, не проходя через закрытые или запрещённые пути."),
    ("question", "npc", "Спрашиваю {npc}: «Что здесь изменилось с момента твоего появления и в чём ты уверен меньше всего?»"),
    ("action", "narrator", "Несколько секунд молчу и наблюдаю за обстановкой и реакциями присутствующих, не приписывая им скрытых мотивов."),
    ("decision", "narrator", "Решаю сначала проверить наблюдаемую деталь среды, а уже потом строить новую версию происходящего."),
    ("action", "narrator", "Сверяю доступные мне обычные инструменты с тем, что вижу вокруг, и пробую только тот безопасный тест, для которого они подходят."),
    ("dialogue", "npc", "Говорю {npc}: «Вот что я пока вижу сам. Скажи, с какой частью моего наблюдения ты не согласен и почему»."),
    ("action", "narrator", "Осторожно проверяю один видимый обычный объект рядом — дверь, ящик, вывеску или механизм, если такой есть, — не объявляя результат заранее."),
    ("question", "npc", "Спрашиваю {npc} о конкретном имени, месте или событии, уже упомянутом в разговоре: «Что именно тебе об этом известно из первых рук?»"),
    ("action", "narrator", "Жду короткое время и смотрю, произойдёт ли что-то наблюдаемое без моего вмешательства. Сам ничего нового не предпринимаю."),
    ("plan", "npc", "Предлагаю {npc} вернуться к самой старой нерешённой детали текущей задачи и проверить её другим способом, вместо того чтобы придумывать новый крючок."),
    ("action", "narrator", "Возвращаю внимание к месту, которое уже осматривал, и сравниваю его текущее состояние с тем, что было описано раньше."),
    ("question", "npc", "Спрашиваю {npc}: «В какой части своего рассказа ты сам сомневаешься? Мне важна именно неопределённость, а не уверенный ответ»."),
    ("action", "narrator", "Определяю источник самой заметной сенсорной детали — звука, запаха, света или движения — если она действительно присутствует в сцене."),
    ("dialogue", "npc", "Говорю {npc}: «Я не прошу доверять мне заранее. Давай проверим одну вещь вместе и посмотрим на результат»."),
    ("decision", "narrator", "Выбираю самый обратимый из доступных следующих шагов: тот, после которого можно остановиться и переоценить ситуацию."),
    ("action", "narrator", "Иду только по явно доступному пути до следующей понятной границы сцены; если путь закрыт, останавливаюсь перед препятствием."),
    ("question", "npc", "Спрашиваю {npc}: «Какое наблюдаемое доказательство заставило бы тебя изменить мнение о происходящем?»"),
    ("action", "narrator", "Осматриваю публичные признаки места — название, герб, номера, объявления, следы владельца или назначения — и читаю только то, что реально доступно."),
    ("dialogue", "npc", "Говорю {npc}: «Меня беспокоит, что мы можем спутать факт и предположение. Назови одно утверждение, которое мы можем проверить прямо сейчас»."),
    ("action", "narrator", "Делаю короткую спокойную передышку: привожу снаряжение в порядок и остаюсь в текущем месте, если обстановка это позволяет."),
    ("plan", "npc", "Предлагаю {npc} сопоставить его версию с тем, что можно увидеть в самой сцене, и не считать расхождение автоматически ложью."),
    ("action", "narrator", "Возвращаюсь к самому раннему конкретному следу, который ещё не получил объяснения, и проверяю только его непосредственное состояние."),
    ("question", "npc", "Спрашиваю {npc}: «Кто здесь обычно принимает решения и откуда ты это знаешь?»"),
    ("decision", "narrator", "Подвожу для себя промежуточный итог только из уже наблюдавшихся фактов и выбираю один следующий проверяемый шаг по текущей цели: {objective}."),
)

_NARRATION_DIAGNOSTICS: dict[int, dict[str, Any]] = {}
_INSTALLED = False


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def contains_session_zero_language(text: str) -> bool:
    return any(pattern.search(text or "") for pattern in SESSION_ZERO_PATTERNS)


def technical_publication_findings(text: str) -> list[str]:
    return [
        label
        for label, pattern in TECHNICAL_PUBLICATION_PATTERNS
        if pattern.search(text or "")
    ]


def scripted_turn_payload(
    turn_number: int,
    active_npcs: list[str] | tuple[str, ...],
    objective: str,
) -> dict[str, str]:
    mode, target_kind, template = SCRIPTED_TURNS[(turn_number - 1) % len(SCRIPTED_TURNS)]
    npcs = sorted({_clean(name) for name in active_npcs if _clean(name)}, key=str.casefold)
    npc = npcs[(turn_number - 1) % len(npcs)] if npcs else None
    objective_short = _clean(objective)[:180] or "текущей цели"

    if target_kind == "npc" and not npc:
        return {
            "target": "narrator",
            "mode": "action",
            "intent": (
                f"Проверяю ещё одну конкретную наблюдаемую деталь текущей сцены, связанную с "
                f"задачей «{objective_short}», не объявляя результат заранее. Ход {turn_number}."
            ),
        }

    target = npc if target_kind == "npc" else "narrator"
    intent = template.format(npc=npc or "собеседник", objective=objective_short)
    return {"target": target, "mode": mode, "intent": intent}


def _attempt_violations(attempts: list[dict], index: int) -> list[dict]:
    for attempt in attempts:
        if int(attempt.get("attempt_index", -1)) == index:
            return list(attempt.get("violations") or [])
    return []


def _repair_candidate(attempts: list[dict]) -> str | None:
    for attempt in attempts:
        if int(attempt.get("attempt_index", -1)) == 1:
            value = str(attempt.get("candidate_text") or "").strip()
            return value or None
    return None


def assess_trace_records(records: list[dict], expected_turns: int = 30) -> dict[str, Any]:
    successful = [record for record in records if not record.get("generation_failed")]
    intents = [
        _clean((record.get("player") or {}).get("intent"))
        for record in records
        if _clean((record.get("player") or {}).get("intent"))
    ]
    fingerprints = {" ".join(re.findall(r"[\w]+", item.casefold())[:60]) for item in intents}
    modes = Counter(str((record.get("player") or {}).get("mode") or "unknown") for record in records)
    scripted = sum((record.get("player") or {}).get("source") == "scripted" for record in records)
    session_zero_turns = [
        int(record.get("turn") or 0)
        for record in records
        if contains_session_zero_language(str((record.get("player") or {}).get("intent") or ""))
    ]

    diagnostics = [record.get("narration") or {} for record in successful]
    missing_diagnostics = sum(not item for item in diagnostics)
    statuses = Counter(str(item.get("protocol_status") or item.get("status") or "missing") for item in diagnostics)
    initial_violations: Counter[str] = Counter()
    repair_violations: Counter[str] = Counter()
    published_leaks: list[dict[str, Any]] = []
    stream_mismatches: list[int] = []
    final_mismatches: list[int] = []

    for record, item in zip(successful, diagnostics, strict=False):
        for violation in item.get("initial_violations") or []:
            initial_violations[str(violation.get("violation_type") or "unknown")] += 1
        for violation in item.get("repair_violations") or []:
            repair_violations[str(violation.get("violation_type") or "unknown")] += 1
        findings = technical_publication_findings(str(item.get("published") or record.get("dm") or ""))
        if findings:
            published_leaks.append({"turn": record.get("turn"), "findings": findings})
        if item and item.get("stream_matches_published") is False:
            stream_mismatches.append(int(record.get("turn") or 0))
        if item and item.get("final_matches_published") is False:
            final_mismatches.append(int(record.get("turn") or 0))

    reasons: list[str] = []
    if scripted != len(records):
        reasons.append(f"scripted player source missing on {len(records) - scripted} turns")
    if session_zero_turns:
        reasons.append(f"session-zero language leaked into turns {session_zero_turns}")
    if successful and missing_diagnostics:
        reasons.append(f"missing narration audit for {missing_diagnostics} successful turns")
    if stream_mismatches:
        reasons.append(f"streamed text differs from saved publication on turns {stream_mismatches}")
    if final_mismatches:
        reasons.append(f"validation final_text differs from publication on turns {final_mismatches}")
    if published_leaks:
        reasons.append(f"technical text reached published narration on {len(published_leaks)} turns")

    diversity_threshold = min(15, expected_turns)
    if len(records) >= min(12, expected_turns):
        if len(fingerprints) < min(diversity_threshold, len(records)):
            reasons.append(f"only {len(fingerprints)} unique player intents")
        if len([mode for mode, count in modes.items() if count]) < 4:
            reasons.append(f"only {len(modes)} player action modes observed")

    repairs = sum(int(item.get("repair_attempts") or 0) > 0 for item in diagnostics)
    safe_fallbacks = sum(str(item.get("protocol_status") or "") == "safe_fallback" for item in diagnostics)
    failed_open = sum(str(item.get("protocol_status") or "") == "failed_open" for item in diagnostics)

    return {
        "valid": not reasons,
        "reasons": reasons,
        "records": len(records),
        "successful": len(successful),
        "scripted_turns": scripted,
        "unique_intents": len(fingerprints),
        "modes": dict(modes),
        "session_zero_turns": session_zero_turns,
        "narration_audits": len(diagnostics) - missing_diagnostics,
        "missing_narration_audits": missing_diagnostics,
        "repairs": repairs,
        "safe_fallbacks": safe_fallbacks,
        "failed_open": failed_open,
        "validation_statuses": dict(statuses),
        "initial_violations": dict(initial_violations),
        "repair_violations": dict(repair_violations),
        "published_technical_leaks": published_leaks,
        "stream_mismatches": stream_mismatches,
        "final_mismatches": final_mismatches,
    }


def install_reliable_soak(facade) -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    original_pair = facade._original_find_logical_pair
    original_report = facade._campaign_report
    base_trace_store = facade.TrackedTraceStore

    async def scripted_generate_player_decision(
        provider,
        config,
        api_key,
        compiler,
        campaign_id,
        phase_runtime,
        player_id,
        history,
        policy,
        turn_number,
        active_theses,
    ):
        del provider, config, api_key, compiler, campaign_id, player_id, history, active_theses
        previous = facade._retry_decision_from_trace()
        if previous is not None:
            return previous
        payload = scripted_turn_payload(
            turn_number,
            list(phase_runtime.phase.active_npcs),
            phase_runtime.phase.objective,
        )
        if contains_session_zero_language(payload["intent"]):
            raise RuntimeError("Reliable soak corpus contains session-zero language")
        decision = facade._BasePlayerDecision(**payload)
        valid, error = policy.validate(decision, list(phase_runtime.phase.active_npcs))
        if not valid:
            raise RuntimeError(f"Reliable soak scripted decision rejected: {error}")
        policy.remember(decision)
        facade.quality.CONTROL_STATS["player_calls"] += 1
        facade.quality.CONTROL_STATS["player_success"] += 1
        facade.quality.CONTROL_STATS["player_scripted"] += 1
        facade.quality._write_health()
        return decision

    async def diagnostic_pair(session, campaign_id, run_id, logical_turn):
        user, assistant = await original_pair(session, campaign_id, run_id, logical_turn)
        if user is None:
            return user, assistant

        result = await session.execute(
            select(NarrationValidationRun)
            .where(NarrationValidationRun.trigger_turn_id == str(user.id))
            .order_by(NarrationValidationRun.updated_at.desc())
        )
        row = result.scalars().first()
        if row is None:
            return user, assistant

        try:
            attempts = json.loads(row.attempts_json or "[]")
        except (json.JSONDecodeError, TypeError):
            attempts = []
        snapshot: dict[str, Any] = {}
        if assistant is not None and assistant.context_snapshot:
            try:
                snapshot = json.loads(assistant.context_snapshot)
            except (json.JSONDecodeError, TypeError):
                snapshot = {}
        provider_telemetry = snapshot.get("provider_telemetry") or {}
        narration_telemetry = provider_telemetry.get("narration_validation") or {}
        protocol = snapshot.get("interagent_protocol") or {}
        published = str(assistant.content or "").strip() if assistant is not None else ""
        final_text = str(row.final_text or "").strip()

        _NARRATION_DIAGNOSTICS[int(logical_turn)] = {
            "validation_run_id": str(row.id),
            "status": row.status,
            "protocol_status": protocol.get("validator_status"),
            "repair_attempts": int(row.repair_attempts or 0),
            "violation_count": int(row.violation_count or 0),
            "failure_reason": row.failure_reason,
            "draft": row.draft_text,
            "repair": _repair_candidate(attempts),
            "published": published,
            "initial_violations": _attempt_violations(attempts, 0),
            "repair_violations": _attempt_violations(attempts, 1),
            "publication_guard": narration_telemetry.get("publication_guard"),
            "final_matches_published": bool(published and final_text == published),
        }
        return user, assistant

    class ReliableTraceStore(base_trace_store):
        def upsert(self, record: dict) -> None:
            player = dict(record.get("player") or {})
            player["source"] = "scripted"
            record["player"] = player
            diagnostic = _NARRATION_DIAGNOSTICS.get(int(record.get("turn") or 0))
            if diagnostic:
                diagnostic = dict(diagnostic)
                diagnostic["stream_matches_published"] = (
                    str(record.get("dm") or "").strip()
                    == str(diagnostic.get("published") or "").strip()
                )
                record["narration"] = diagnostic
            super().upsert(record)

    def reliable_report(database_path, data_dir):
        trace = facade._BaseTraceStore(data_dir / "realistic_simulation_trace.jsonl")
        records = list(trace.records.values())
        expected_turns = max(1, int(os.getenv("PDM_SIM_TURNS", "30")))
        assessment = assess_trace_records(records, expected_turns=expected_turns)
        lines = original_report(database_path, data_dir)
        harness_lines = [
            "- Reliable soak harness: **{}**".format("VALID" if assessment["valid"] else "INVALID"),
            "- Harness invalid reasons: " + ("; ".join(assessment["reasons"]) if assessment["reasons"] else "none"),
            f"- Scripted player turns: {assessment['scripted_turns']}/{assessment['records']}",
            f"- Unique scripted intents: {assessment['unique_intents']}",
            f"- Scripted modes: `{json.dumps(assessment['modes'], ensure_ascii=False, sort_keys=True)}`",
            f"- Session-zero contamination turns: {assessment['session_zero_turns'] or 'none'}",
            f"- Narration audits: {assessment['narration_audits']}/{assessment['successful']}",
            f"- Narration repairs: {assessment['repairs']}/{assessment['narration_audits']}",
            f"- Safe authority projections: {assessment['safe_fallbacks']}",
            f"- Validator failed_open: {assessment['failed_open']}",
            f"- Validator statuses: `{json.dumps(assessment['validation_statuses'], ensure_ascii=False, sort_keys=True)}`",
            f"- Initial violation types: `{json.dumps(assessment['initial_violations'], ensure_ascii=False, sort_keys=True)}`",
            f"- Repair violation types: `{json.dumps(assessment['repair_violations'], ensure_ascii=False, sort_keys=True)}`",
            f"- Published technical leaks: {len(assessment['published_technical_leaks'])}",
            f"- Stream/publication mismatches: {assessment['stream_mismatches'] or 'none'}",
            f"- Validation-final/publication mismatches: {assessment['final_mismatches'] or 'none'}",
        ]
        return [*harness_lines, *lines]

    facade._original_generate_player_decision = scripted_generate_player_decision
    facade._original_find_logical_pair = diagnostic_pair
    facade.TrackedTraceStore = ReliableTraceStore
    facade._campaign_report = reliable_report


__all__ = [
    "SCRIPTED_TURNS",
    "assess_trace_records",
    "contains_session_zero_language",
    "install_reliable_soak",
    "scripted_turn_payload",
    "technical_publication_findings",
]
