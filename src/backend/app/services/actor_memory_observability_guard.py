from __future__ import annotations

import json
import re
from contextvars import ContextVar
from uuid import UUID

from app.models.turn import ChatMessage
from app.providers.llm_provider import LLMProviderError
from app.services.actor_turn_authority_guard import (
    ActorSegmentSelection,
    build_actor_segment_proposals,
    segment_actor_response,
)
from app.services.playtest_trace import PlaytestTraceService
from app.services.role_model_router import ModelRole

_INSTALLED = False
_ACTOR_AUDIT: ContextVar[dict | None] = ContextVar("actor_memory_audit", default=None)
_SILENCE_PATTERN = re.compile(
    r"\b(?:молчит|умолкает|не\s+отвечает|ничего\s+не\s+говорит|"
    r"silent|says\s+nothing|does\s+not\s+answer)\b",
    flags=re.IGNORECASE,
)


def _set_audit(scribe, audit: dict) -> None:
    safe = dict(audit)
    _ACTOR_AUDIT.set(safe)
    current = dict(getattr(scribe, "last_audit", {}) or {})
    current.update(safe)
    scribe.last_audit = current


async def extract_actor_segment_proposals_with_audit(
    scribe,
    *,
    campaign_id: UUID,
    assistant_content: str,
    acting_character_id: UUID,
    player_character_id: UUID,
):
    """Select immutable actor claims with one bounded semantic retry and durable diagnostics."""
    clean = " ".join((assistant_content or "").split()).strip()
    base_audit = {
        "actor_knowledge_mode": "indexed_segments",
        "actor_generic_scribe_skipped": True,
        "actor_id": str(acting_character_id),
        "recipient_id": str(player_character_id),
        "selector_attempts": 0,
        "selector_status": "not_started",
        "selector_error": None,
        "candidate_segments": [],
        "selected_segment_ids": [],
    }
    if not clean or (_SILENCE_PATTERN.search(clean) and len(clean) < 180):
        base_audit["selector_status"] = "skipped_silence"
        _set_audit(scribe, base_audit)
        return []

    actor = await scribe._entity_repo.get_character(acting_character_id)  # noqa: SLF001
    player = await scribe._entity_repo.get_character(player_character_id)  # noqa: SLF001
    if not actor or not player:
        base_audit["selector_status"] = "skipped_missing_actor_or_recipient"
        _set_audit(scribe, base_audit)
        return []

    segments = segment_actor_response(assistant_content)
    base_audit["candidate_segments"] = [
        {"segment_id": index, "text": segment}
        for index, segment in enumerate(segments, start=1)
    ]
    if not segments:
        base_audit["selector_status"] = "skipped_no_segments"
        _set_audit(scribe, base_audit)
        return []

    selection = await scribe._model_router.resolve(campaign_id, ModelRole.SCRIBE)  # noqa: SLF001
    if selection is None:
        base_audit["selector_status"] = "skipped_no_scribe_model"
        _set_audit(scribe, base_audit)
        return []

    segment_block = "\n".join(
        f"S{index}: {segment}" for index, segment in enumerate(segments, start=1)
    )
    system_prompt = (
        "[ACTOR CLAIM SEGMENT SELECTOR]\n"
        "Тебе даны неизменяемые фрагменты ОПУБЛИКОВАННОГО ответа NPC. Не пиши и не "
        "исправляй текст. Верни только номера S-сегментов, в которых сам выбранный NPC "
        "сообщает персонажу игрока конкретное фактическое сведение о человеке, месте, "
        "предмете, событии, времени, доступе, внешности или наблюдении. Не выбирай жесты, "
        "эмоции, атмосферу, Narrator-текст, вопросы, приветствия или чистые намерения. "
        "Явное отрицательное утверждение NPC допустимо. Не решай, прав ли NPC: это только "
        "character_claim. Если фактических утверждений нет, верни пустой список.\n"
        f"Говорящий NPC: {actor.canonical_name}.\n"
        f"Слушатель: {player.canonical_name}.\n"
        "Формат: {\"segment_ids\":[1,2]}"
    )

    async def select_ids(extra_instruction: str | None = None) -> list[int]:
        messages = [
            ChatMessage(role="system", content=system_prompt),
            ChatMessage(role="user", content=segment_block),
        ]
        if extra_instruction:
            messages.append(ChatMessage(role="user", content=extra_instruction))
        data = await scribe._model_router.generate_json(  # noqa: SLF001
            scribe._llm_provider,  # noqa: SLF001
            selection,
            messages,
            max_tokens=220,
            temperature=0.0,
            response_model=ActorSegmentSelection,
        )
        envelope = ActorSegmentSelection.model_validate(data)
        return list(envelope.segment_ids)

    selected_ids: list[int] = []
    first_error: str | None = None
    try:
        base_audit["selector_attempts"] = 1
        selected_ids = await select_ids()
    except (LLMProviderError, ValueError, TypeError) as exc:
        first_error = str(exc)[:1200]

    # One background-only retry is safe: Qwen still selects immutable IDs and can never rewrite
    # evidence. Retry both malformed/provider failures and suspicious empty selections on a
    # substantive actor response; this removes the intermittent Round-28 dropout without adding
    # interactive latency or weakening the evidence boundary.
    should_retry_empty = (
        not selected_ids
        and first_error is None
        and any(len(segment.split()) >= 6 for segment in segments)
    )
    if first_error is not None or should_retry_empty:
        try:
            base_audit["selector_attempts"] = 2
            selected_ids = await select_ids(
                "Перепроверь сегменты один раз. Пустой список допустим только если NPC действительно "
                "не сообщил ни одного конкретного фактического утверждения. Не добавляй текста и "
                "не меняй полярность; выбери только существующие номера S."
            )
        except (LLMProviderError, ValueError, TypeError) as exc:
            base_audit["selector_error"] = str(exc)[:1200]
            base_audit["selector_status"] = "selector_failed"
            _set_audit(scribe, base_audit)
            return []

    proposals = build_actor_segment_proposals(
        segments,
        selected_ids,
        acting_character_id=acting_character_id,
        player_character_id=player_character_id,
    )
    accepted_ids = [
        int((proposal.payload.get("_canon") or {}).get("segment_id"))
        for proposal in proposals
        if (proposal.payload.get("_canon") or {}).get("segment_id") is not None
    ]
    base_audit["selected_segment_ids"] = accepted_ids
    base_audit["selector_error"] = first_error
    base_audit["selector_status"] = "selected" if proposals else "empty_selection"
    base_audit["actor_evidence_knowledge_created"] = len(proposals)
    _set_audit(scribe, base_audit)
    return proposals


async def _persist_actor_audit(processor, job_id: UUID, audit: dict) -> None:
    from app.db.tables import PostTurnJob, Turn

    job = await processor._session.get(PostTurnJob, str(job_id))  # noqa: SLF001
    if not job or job.job_type != "memory_scribe" or not job.assistant_turn_id:
        return
    turn = await processor._session.get(Turn, job.assistant_turn_id)  # noqa: SLF001
    if not turn:
        return
    try:
        snapshot = json.loads(turn.context_snapshot or "{}")
    except (json.JSONDecodeError, TypeError):
        snapshot = {}
    if not isinstance(snapshot, dict):
        snapshot = {}
    snapshot["actor_memory_debug"] = audit
    turn.context_snapshot = json.dumps(snapshot, ensure_ascii=False)
    await processor._session.commit()  # noqa: SLF001


def _augment_trace(snapshot: dict, assistant_turn_id: str, trace: dict) -> dict:
    assistant = next(
        (
            turn
            for turn in snapshot.get("turns", [])
            if turn.get("id") == assistant_turn_id
        ),
        None,
    )
    if not assistant:
        return trace
    context = assistant.get("context_snapshot") or {}
    audit = context.get("actor_memory_debug") if isinstance(context, dict) else None

    if not isinstance(audit, dict) and assistant.get("actor_id"):
        candidate_segments = segment_actor_response(str(assistant.get("content") or ""))
        selected_ids: list[int] = []
        for proposal in snapshot.get("proposals", []):
            if proposal.get("turn_id") != assistant_turn_id or proposal.get("change_type") != "knowledge":
                continue
            payload = proposal.get("payload") or {}
            canon = payload.get("_canon") if isinstance(payload, dict) else None
            value = canon.get("segment_id") if isinstance(canon, dict) else None
            try:
                if value is not None:
                    selected_ids.append(int(value))
            except (TypeError, ValueError):
                pass
        audit = {
            "selector_status": "legacy_trace_inferred",
            "candidate_segments": [
                {"segment_id": index, "text": segment}
                for index, segment in enumerate(candidate_segments, start=1)
            ],
            "selected_segment_ids": selected_ids,
            "selector_attempts": None,
            "selector_error": None,
        }

    trace.setdefault("memory", {})["actor_selector"] = audit or {}
    return trace


def install() -> None:
    """Install actor-claim retry/audit without changing the immutable evidence contract."""
    global _INSTALLED
    if _INSTALLED:
        return

    import app.services.post_turn_processor as post_turn_module
    from app.services.post_turn_processor import PostTurnProcessor

    original_process_job = PostTurnProcessor.process_job
    original_trace = PlaytestTraceService._trace_from_snapshot

    post_turn_module.extract_actor_segment_proposals = extract_actor_segment_proposals_with_audit

    async def audited_process_job(self, job_id, *, already_claimed=False):
        token = _ACTOR_AUDIT.set(None)
        try:
            return await original_process_job(
                self,
                job_id,
                already_claimed=already_claimed,
            )
        finally:
            audit = _ACTOR_AUDIT.get()
            if audit:
                try:
                    await _persist_actor_audit(self, job_id, audit)
                except Exception:  # debug persistence must never turn a completed memory job into failure
                    await self._session.rollback()  # noqa: SLF001
            _ACTOR_AUDIT.reset(token)

    @staticmethod
    def traced_snapshot(snapshot, assistant_turn_id):
        trace = original_trace(snapshot, assistant_turn_id)
        if trace is None:
            return None
        return _augment_trace(snapshot, assistant_turn_id, trace)

    PostTurnProcessor.process_job = audited_process_job
    PlaytestTraceService._trace_from_snapshot = traced_snapshot
    _INSTALLED = True


__all__ = [
    "extract_actor_segment_proposals_with_audit",
    "install",
]
