from __future__ import annotations

import re
from collections import Counter
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

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


class PlaytestTraceService:
    """Read-only causal flight recorder assembled from durable turn evidence.

    This service deliberately does not add another runtime write path. It explains what the
    existing pipeline already persisted so a live playtest can identify the first layer where a
    turn diverged: input routing, planner, authority, narration, validation, memory, or commit.
    """

    def __init__(self, session: AsyncSession):
        self._session = session

    async def turn_trace(self, campaign_id: UUID, assistant_turn_id: UUID) -> dict:
        snapshot = await DebuggerService(self._session).snapshot(campaign_id, turn_limit=1000)
        trace = self._trace_from_snapshot(snapshot, str(assistant_turn_id))
        if trace is None:
            raise ValueError("Assistant turn not found in campaign")
        return trace

    async def campaign_trace(self, campaign_id: UUID, turn_limit: int = 100) -> dict:
        snapshot = await DebuggerService(self._session).snapshot(campaign_id, turn_limit=turn_limit)
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
        return {
            "campaign": snapshot.get("campaign"),
            "health": snapshot.get("health", {}),
            "summary": {
                "assistant_turns": len(traces),
                "diagnostic_flags": dict(sorted(flags.items())),
                "validator_statuses": dict(sorted(validator_statuses.items())),
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
                "status": protocol.get("validator_status")
                or narration_validation.get("status"),
                "validation_run_id": narration_validation.get("validation_run_id"),
                "telemetry": narration_validation,
            },
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
