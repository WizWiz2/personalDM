from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime
from difflib import SequenceMatcher
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.narration_validation_table import NarrationValidationRun
from app.services.context_compiler import count_tokens
from app.services.debugger_service import DebuggerService


_ACTION_RE = re.compile(
    r"\b(?:иду|идем|идём|пойду|выхожу|выходим|направляюсь|отправляюсь|"
    r"возвращаюсь|перехожу|подхожу|отхожу|ухожу|покидаю|захожу|вхожу|"
    r"осматриваю|беру|кладу|открываю|закрываю|ищу|проверяю|делаю|пытаюсь)\b",
    flags=re.IGNORECASE,
)
_MOVEMENT_RE = re.compile(
    r"\b(?:выхо(?:жу|дит)|ухо(?:жу|дит)|покида(?:ю|ет)|направля(?:юсь|ется)|"
    r"отправля(?:юсь|ется)|перехо(?:жу|дит)|возвраща(?:юсь|ется)|вхо(?:жу|дит)|"
    r"прихо(?:жу|дит)|иду|идет|идёт|пойду)\b",
    flags=re.IGNORECASE,
)
_SILENCE_RE = re.compile(
    r"\b(?:молчит|умолкает|не\s+отвечает|ничего\s+не\s+говорит)\b",
    flags=re.IGNORECASE,
)
_TECHNICAL_RE = re.compile(
    r"(?:\[Generation failed|Traceback|Pydantic|validation error|UUID\(|"
    r"finish_reason|LLMProvider|JSONDecodeError)",
    flags=re.IGNORECASE,
)
_OBJECTIVE_CHANGE_TYPES = {"fact", "event", "relationship", "movement", "item_transfer"}
_BAD_PUBLICATION_MODES = {"safe_fallback", "presentation_fallback", "failed_open"}


def _elapsed_seconds(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    try:
        return round(
            (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds(),
            3,
        )
    except (TypeError, ValueError):
        return None


def _json(value: str | None, fallback):
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _repair_preservation_ratio(audit: dict | None) -> float | None:
    if not audit:
        return None
    draft = str(audit.get("draft_text") or "").strip()
    final = str(audit.get("final_text") or "").strip()
    if not draft or not final:
        return None
    return round(SequenceMatcher(None, draft, final).ratio(), 3)


def _quality_classification(
    audit: dict | None,
    publication_mode: str,
    assistant_text: str,
    diagnostics: list[dict],
) -> dict:
    if audit is None:
        raw = "unknown"
        raw_basis = "no persisted NarrationValidationRun"
    else:
        attempts = audit.get("attempts") or []
        first = attempts[0] if attempts and isinstance(attempts[0], dict) else {}
        first_status = str(
            first.get("status")
            or first.get("verdict")
            or first.get("result")
            or ""
        ).casefold()
        raw_bad = bool(audit.get("violation_count")) or first_status in {
            "fail",
            "failed",
            "reject",
            "rejected",
            "invalid",
        }
        raw = "bad" if raw_bad else "good"
        raw_basis = "persisted validator first-pass evidence"

    diagnostic_errors = {
        str(item.get("code"))
        for item in diagnostics
        if str(item.get("severity")) == "error"
    }
    published_bad = (
        not assistant_text.strip()
        or publication_mode in _BAD_PUBLICATION_MODES
        or "TECHNICAL_LEAK" in diagnostic_errors
    )
    published = "bad" if published_bad else "good"
    label = f"RAW {raw.upper()}/PUBLISHED {published.upper()}"
    return {
        "class": label,
        "raw": raw,
        "published": published,
        "raw_basis": raw_basis,
        "published_basis": "publication mode + deterministic surface diagnostics",
        "repair_preservation_ratio": _repair_preservation_ratio(audit),
    }


class PlaytestTraceService:
    """Read-only causal flight recorder assembled from durable turn evidence.

    This service deliberately does not add another runtime write path. It explains what the
    existing pipeline already persisted so a live playtest can identify the first layer where a
    turn diverged: input routing, planner, authority, narration, validation, memory, or commit.
    """

    def __init__(self, session: AsyncSession):
        self._session = session

    async def _validation_audits(
        self,
        campaign_id: UUID,
        trigger_turn_ids: set[str],
    ) -> list[dict]:
        if not trigger_turn_ids:
            return []
        rows = (
            await self._session.execute(
                select(NarrationValidationRun)
                .where(
                    NarrationValidationRun.campaign_id == str(campaign_id),
                    NarrationValidationRun.trigger_turn_id.in_(sorted(trigger_turn_ids)),
                )
                .order_by(NarrationValidationRun.created_at)
            )
        ).scalars().all()
        return [
            {
                "id": row.id,
                "campaign_id": row.campaign_id,
                "trigger_turn_id": row.trigger_turn_id,
                "assistant_turn_id": row.assistant_turn_id,
                "scene_id": row.scene_id,
                "status": row.status,
                "draft_text": row.draft_text,
                "final_text": row.final_text,
                "attempts": _json(row.attempts_json, []),
                "violation_count": row.violation_count,
                "repair_attempts": row.repair_attempts,
                "validator_model_name": row.validator_model_name,
                "failure_reason": row.failure_reason,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }
            for row in rows
        ]

    async def turn_trace(self, campaign_id: UUID, assistant_turn_id: UUID) -> dict:
        snapshot = await DebuggerService(self._session).snapshot(campaign_id, turn_limit=1000)
        trigger_turn_ids = {
            str(turn.get("id"))
            for turn in snapshot.get("turns", [])
            if turn.get("role") == "user" and turn.get("id")
        }
        snapshot["_narration_validation_audits"] = await self._validation_audits(
            campaign_id,
            trigger_turn_ids,
        )
        trace = self._trace_from_snapshot(snapshot, str(assistant_turn_id))
        if trace is None:
            raise ValueError("Assistant turn not found in campaign")
        return trace

    async def campaign_trace(self, campaign_id: UUID, turn_limit: int = 100) -> dict:
        snapshot = await DebuggerService(self._session).snapshot(campaign_id, turn_limit=turn_limit)
        trigger_turn_ids = {
            str(turn.get("id"))
            for turn in snapshot.get("turns", [])
            if turn.get("role") == "user" and turn.get("id")
        }
        snapshot["_narration_validation_audits"] = await self._validation_audits(
            campaign_id,
            trigger_turn_ids,
        )
        traces = [
            trace
            for turn in snapshot.get("turns", [])
            if turn.get("role") == "assistant"
            for trace in [self._trace_from_snapshot(snapshot, turn["id"])]
            if trace is not None
        ]
        flags = Counter(
            flag["code"]
            for trace in traces
            for flag in trace.get("diagnostics", [])
        )
        latencies = [
            value
            for trace in traces
            for value in [trace.get("timing", {}).get("interactive_seconds")]
            if isinstance(value, (int, float))
        ]
        validator_statuses = Counter(
            str(trace.get("validator", {}).get("status") or "unknown") for trace in traces
        )
        publication_modes = Counter(
            str(trace.get("publication", {}).get("mode") or "unknown") for trace in traces
        )
        quality_classes = Counter(
            str(trace.get("quality", {}).get("class") or "unknown") for trace in traces
        )
        return {
            "campaign": snapshot.get("campaign"),
            "health": snapshot.get("health", {}),
            "summary": {
                "assistant_turns": len(traces),
                "diagnostic_flags": dict(sorted(flags.items())),
                "validator_statuses": dict(sorted(validator_statuses.items())),
                "publication_modes": dict(sorted(publication_modes.items())),
                "raw_published_classes": dict(sorted(quality_classes.items())),
                "interactive_seconds": {
                    "min": min(latencies) if latencies else None,
                    "max": max(latencies) if latencies else None,
                    "average": (
                        round(sum(latencies) / len(latencies), 3) if latencies else None
                    ),
                },
            },
            "turns": traces,
        }

    @staticmethod
    def _trace_from_snapshot(snapshot: dict, assistant_turn_id: str) -> dict | None:
        turns = snapshot.get("turns", [])
        by_id = {turn.get("id"): turn for turn in turns}
        assistant = by_id.get(assistant_turn_id)
        if not assistant or assistant.get("role") != "assistant":
            return None
        user = by_id.get(assistant.get("parent_turn_id"))
        context = assistant.get("context_snapshot") or {}
        planner = context.get("turn_planner") or {}
        authority = context.get("turn_authority") or {}
        transition = context.get("scene_transition") or {}
        materialization = context.get("turn_materialization") or {}
        protocol = context.get("interagent_protocol") or {}
        provider = context.get("provider_telemetry") or {}

        proposals = [
            item
            for item in snapshot.get("proposals", [])
            if item.get("turn_id") == assistant_turn_id
        ]
        beliefs = [
            item
            for item in snapshot.get("beliefs", [])
            if item.get("source_turn_id") == assistant_turn_id
        ]
        facts = [
            item
            for item in snapshot.get("facts", [])
            if item.get("source_turn_id") == assistant_turn_id
        ]
        relationships = [
            item
            for item in snapshot.get("relationships", [])
            if item.get("source_turn_id") == assistant_turn_id
        ]
        events = [
            item
            for item in snapshot.get("events", [])
            if assistant_turn_id in (item.get("source_turns") or [])
        ]
        jobs = [
            item
            for item in snapshot.get("post_turn_jobs", [])
            if item.get("assistant_turn_id") == assistant_turn_id
        ]
        generation = next(
            (
                item
                for item in snapshot.get("generation_runs", [])
                if item.get("assistant_turn_id") == assistant_turn_id
                or (user and item.get("user_turn_id") == user.get("id"))
            ),
            None,
        )

        actor_id = assistant.get("actor_id")
        planner_reason = str(planner.get("reason") or "")
        planner_called = not (
            str(planner.get("status") or "") == "skipped"
            and planner_reason == "actor_scoped_turn"
        )
        knowledge_proposals = [
            item for item in proposals if item.get("change_type") == "knowledge"
        ]
        objective_proposals = [
            item
            for item in proposals
            if item.get("change_type") in _OBJECTIVE_CHANGE_TYPES
        ]
        memory_job = next(
            (item for item in jobs if item.get("job_type") == "memory_scribe"),
            None,
        )

        diagnostics: list[dict] = []
        user_text = str((user or {}).get("content") or "")
        assistant_text = str(assistant.get("content") or "")
        if actor_id and not planner_called and _ACTION_RE.search(user_text):
            diagnostics.append(
                {
                    "code": "PLANNER_BYPASSED_WITH_ACTION_LANGUAGE",
                    "severity": "error",
                    "detail": (
                        "Input was actor-scoped while also containing player-action language; "
                        "the Planner was skipped."
                    ),
                }
            )
        if (
            actor_id
            and memory_job
            and memory_job.get("status") == "completed"
            and not knowledge_proposals
            and not beliefs
            and not _SILENCE_RE.search(assistant_text)
        ):
            diagnostics.append(
                {
                    "code": "ACTOR_MEMORY_DROPOUT",
                    "severity": "warning",
                    "detail": (
                        "Actor turn completed memory processing but produced no knowledge proposal "
                        "or persisted belief."
                    ),
                }
            )
        if actor_id and objective_proposals:
            diagnostics.append(
                {
                    "code": "OBJECTIVE_CANON_FROM_ACTOR_SPEECH",
                    "severity": "warning",
                    "detail": (
                        "Actor-scoped turn produced objective canon proposals: "
                        + ", ".join(sorted({item["change_type"] for item in objective_proposals}))
                    ),
                }
            )
        transition_status = str(transition.get("status") or "")
        has_structured_transition = bool(
            transition.get("target_scene_id")
            or transition.get("transition_id")
            or transition_status in {"prepared", "reused", "applied"}
        )
        if (
            not has_structured_transition
            and _MOVEMENT_RE.search(user_text)
            and _MOVEMENT_RE.search(assistant_text)
        ):
            diagnostics.append(
                {
                    "code": "PROSE_STATE_DIVERGENCE",
                    "severity": "warning",
                    "detail": (
                        "Player requested movement and published prose describes movement, but no "
                        "structured scene transition is recorded."
                    ),
                }
            )
        if _TECHNICAL_RE.search(assistant_text):
            diagnostics.append(
                {
                    "code": "TECHNICAL_LEAK",
                    "severity": "error",
                    "detail": "Published assistant text contains a technical diagnostic marker.",
                }
            )

        interactive_seconds = _elapsed_seconds(
            (user or {}).get("created_at"), assistant.get("created_at")
        )
        if interactive_seconds is not None and interactive_seconds > 60:
            diagnostics.append(
                {
                    "code": "SLOW_TURN",
                    "severity": "warning",
                    "detail": f"Interactive turn took {interactive_seconds:.1f}s before persistence.",
                }
            )

        narration_validation = provider.get("narration_validation") or {}
        repetition = provider.get("repetition_guard") or {}
        persisted_run_id = str(narration_validation.get("validation_run_id") or "")
        user_turn_id = str((user or {}).get("id") or "")
        audit_runs = [
            item
            for item in (snapshot.get("_narration_validation_audits") or [])
            if (
                (persisted_run_id and str(item.get("id") or "") == persisted_run_id)
                or str(item.get("assistant_turn_id") or "") == assistant_turn_id
                or (user_turn_id and str(item.get("trigger_turn_id") or "") == user_turn_id)
            )
        ]
        validation_audit = None
        if persisted_run_id:
            validation_audit = next(
                (
                    item
                    for item in reversed(audit_runs)
                    if str(item.get("id") or "") == persisted_run_id
                ),
                None,
            )
        if validation_audit is None and audit_runs:
            validation_audit = audit_runs[-1]

        publication_mode = str(
            protocol.get("validator_status")
            or narration_validation.get("publication_mode")
            or narration_validation.get("status")
            or "unknown"
        )
        durable_validator_status = (
            (validation_audit or {}).get("status")
            or narration_validation.get("status")
            or "unknown"
        )
        quality = _quality_classification(
            validation_audit,
            publication_mode,
            assistant_text,
            diagnostics,
        )

        context_breakdown = dict(context.get("token_budget_breakdown") or {})
        authority_tokens = (
            count_tokens(json.dumps(authority, ensure_ascii=False, sort_keys=True))
            if authority
            else 0
        )
        context_breakdown["authority"] = authority_tokens
        context_breakdown["total_with_authority_estimate"] = (
            int(context_breakdown.get("total_prompt_estimate") or 0) + authority_tokens
        )
        planner_telemetry = planner.get("telemetry") or {}
        token_budget = {
            "compiled_max": context.get("token_budget_max"),
            "compiled_used_before_authority": context.get("token_budget_used"),
            "components": context_breakdown,
            "included_layers": context.get("included_layers") or [],
            "reserves": {
                "narrator_requested_max_tokens": provider.get("requested_max_tokens"),
                "narrator_requested_context": provider.get("requested_num_ctx"),
                "planner_requested_max_tokens": (
                    planner_telemetry.get("requested_max_tokens")
                    if isinstance(planner_telemetry, dict)
                    else None
                ),
                "planner_requested_context": (
                    planner_telemetry.get("requested_num_ctx")
                    if isinstance(planner_telemetry, dict)
                    else None
                ),
            },
            "provider_usage": provider.get("usage") or {},
            "planner_provider_usage": (
                planner_telemetry.get("usage")
                if isinstance(planner_telemetry, dict)
                else {}
            )
            or {},
        }

        return {
            "assistant_turn_id": assistant_turn_id,
            "scene": {
                "id": assistant.get("scene_id"),
                "title": assistant.get("scene_title"),
            },
            "input": {
                "user_turn_id": (user or {}).get("id"),
                "text": user_text,
                "acting_character_id": actor_id,
                "acting_character_name": assistant.get("actor_name"),
            },
            "routing": {
                "planner_called": planner_called,
                "planner_status": planner.get("status"),
                "planner_skip_reason": planner.get("reason"),
            },
            "planner": planner,
            "authority": authority,
            "transition": transition,
            "materialization": materialization,
            "narrator": {
                "model": assistant.get("context_snapshot", {})
                .get("provider_telemetry", {})
                .get("model")
                or assistant.get("model_name"),
                "telemetry": provider,
                "published_text": assistant_text,
                "repetition_guard": repetition,
            },
            "validator": {
                "status": durable_validator_status,
                "runtime_status": protocol.get("validator_status"),
                "validation_run_id": (
                    (validation_audit or {}).get("id")
                    or narration_validation.get("validation_run_id")
                ),
                "telemetry": narration_validation,
                "audit": validation_audit,
                "runs_for_turn": audit_runs,
            },
            "publication": {
                "mode": publication_mode,
                "degraded": bool(
                    provider.get("narration_degraded")
                    or publication_mode in {"safe_fallback", "presentation_fallback"}
                ),
                "guard": narration_validation.get("publication_guard"),
                "published_text": assistant_text,
            },
            "quality": quality,
            "token_budget": token_budget,
            "memory": {
                "job": memory_job,
                "proposal_count": len(proposals),
                "knowledge_proposal_count": len(knowledge_proposals),
                "objective_proposals": objective_proposals,
                "persisted": {
                    "beliefs": beliefs,
                    "facts": facts,
                    "relationships": relationships,
                    "events": events,
                },
            },
            "generation": generation,
            "timing": {"interactive_seconds": interactive_seconds},
            "diagnostics": diagnostics,
        }


__all__ = ["PlaytestTraceService"]
