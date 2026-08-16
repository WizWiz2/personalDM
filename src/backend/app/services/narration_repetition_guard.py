from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import wraps
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.turn_repo import TurnRepository
from app.models.turn import ChatMessage
from app.models.turn_authority import TurnAuthority

_WORD_RE = re.compile(r"[\w]+", re.UNICODE)
_INSTALLED = False


@dataclass(frozen=True)
class RepetitionMatch:
    previous_text: str
    similarity: float
    exact: bool


class NarrationRepetitionGuard:
    """Detect near-verbatim publication loops without deciding new story content.

    Actor turns get a slightly lower threshold because repeated dialogue is particularly visible.
    Ordinary narration uses a stricter threshold to avoid treating recurring scene vocabulary as a
    loop. The guard never requires a new fact: a stubborn NPC may preserve the same position, but
    the renderer must react to the current input instead of replaying the same paragraph.
    """

    RECENT_LIMIT = 4
    ACTOR_THRESHOLD = 0.86
    NARRATOR_THRESHOLD = 0.93

    def __init__(self, session: AsyncSession):
        self._turns = TurnRepository(session)

    @staticmethod
    def _normalized(text: str) -> str:
        return " ".join(_WORD_RE.findall((text or "").casefold()))

    @classmethod
    def _similarity(cls, left: str, right: str) -> float:
        a = cls._normalized(left)
        b = cls._normalized(right)
        if not a or not b:
            return 0.0
        if a == b:
            return 1.0

        # Short conversational refusals should only match when effectively identical. Longer
        # paragraphs tolerate small model-authored punctuation/adjective changes.
        if min(len(a), len(b)) < 32:
            return SequenceMatcher(None, a, b, autojunk=False).ratio()

        sequence_ratio = SequenceMatcher(None, a, b, autojunk=False).ratio()
        a_tokens = a.split()
        b_tokens = b.split()
        a_pairs = set(zip(a_tokens, a_tokens[1:]))
        b_pairs = set(zip(b_tokens, b_tokens[1:]))
        if not a_pairs or not b_pairs:
            return sequence_ratio
        pair_ratio = len(a_pairs & b_pairs) / len(a_pairs | b_pairs)
        return max(sequence_ratio, pair_ratio)

    async def recent_responses(
        self,
        campaign_id: UUID,
        scene_id: UUID | None,
        authority: TurnAuthority,
    ) -> list[str]:
        history = await self._turns.get_history(
            campaign_id,
            limit=60,
            active_only=True,
        )
        actor_id = authority.acting_character_id
        selected: list[str] = []
        for turn in reversed(history):
            if turn.role != "assistant":
                continue
            if actor_id is not None:
                if turn.acting_character_id != actor_id:
                    continue
            else:
                if turn.acting_character_id is not None:
                    continue
                if scene_id is not None and turn.scene_id != scene_id:
                    continue
            selected.append(turn.content)
            if len(selected) >= self.RECENT_LIMIT:
                break
        return selected

    def detect(
        self,
        candidate: str,
        previous_responses: list[str],
        *,
        actor_turn: bool,
    ) -> RepetitionMatch | None:
        threshold = self.ACTOR_THRESHOLD if actor_turn else self.NARRATOR_THRESHOLD
        best: RepetitionMatch | None = None
        normalized_candidate = self._normalized(candidate)
        for previous in previous_responses:
            normalized_previous = self._normalized(previous)
            if not normalized_previous:
                continue
            score = self._similarity(candidate, previous)
            exact = normalized_candidate == normalized_previous
            if not exact and min(len(normalized_candidate), len(normalized_previous)) < 32:
                # Avoid forcing variety on ordinary short answers such as "Нет" / "Не знаю".
                continue
            if score < threshold:
                continue
            match = RepetitionMatch(
                previous_text=previous,
                similarity=score,
                exact=exact,
            )
            if best is None or match.similarity > best.similarity:
                best = match
        return best

    @staticmethod
    def retry_messages(
        messages: list[ChatMessage],
        authority: TurnAuthority,
        match: RepetitionMatch,
    ) -> list[ChatMessage]:
        actor_turn = authority.scene_disposition == "actor_turn"
        speaker = authority.acting_character_name or "этот же рассказчик"
        if actor_turn:
            instruction = (
                f"Выбранный собеседник: {speaker}. Сохрани его текущую позицию и знания. "
                "Если у него нет новой информации, это нормально: коротко и естественно покажи, "
                "что позиция не изменилась, но ответь на текущую реплику иначе. Не выдумывай новый "
                "факт только ради разнообразия. Не говори и не действуй за героя игрока."
            )
        else:
            instruction = (
                "Сохрани ровно тот же авторизованный игровой исход, но отреагируй на текущий ввод "
                "новой формулировкой. Не повторяй прежний абзац, не добавляй новый факт или событие "
                "только ради разнообразия и не управляй героем игрока."
            )
        previous = " ".join(match.previous_text.split())[:1800]
        return [
            *messages,
            ChatMessage(
                role="user",
                content=(
                    "[REPETITION GUARD]\n"
                    "Новый черновик почти дословно повторил уже опубликованный ответ того же "
                    "говорящего. Сгенерируй текущий ответ с нуля.\n"
                    f"Ранее уже опубликовано: {previous}\n"
                    f"{instruction}"
                ),
            ),
        ]


def install() -> None:
    """Keep the normal authority-validation receipt when repetition falls back to Authority.

    Repetition screening is an explicit narration stage. This small runtime hook only preserves the
    stronger audit invariant: every published turn still passes through the real authority validator,
    even when the prose model repeated itself twice and the publication candidate is replaced by a
    deterministic TurnAuthority projection.
    """
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    from app.services.authority_narration_pipeline import (
        AuthorityNarrationPipeline,
        AuthorityNarrationResult,
    )
    from app.services.narration_publication_guard import NarrationPublicationGuard
    from app.services.narration_validator import NarrationValidationError
    from app.services.role_model_router import ModelRole
    from app.services.turn_authority_validator import TurnAuthorityValidator

    original_publish_fallback = AuthorityNarrationPipeline._publish_fallback

    @wraps(original_publish_fallback)
    async def validated_repetition_fallback(self, *args, **kwargs):
        reason = str(kwargs.get("reason") or "")
        if "repetition" not in reason:
            return await original_publish_fallback(self, *args, **kwargs)

        audit = kwargs.get("audit")
        run = kwargs.get("run")
        authority = kwargs.get("authority")
        if audit is None or run is None or authority is None:
            return await original_publish_fallback(self, *args, **kwargs)

        campaign_id = UUID(str(run.campaign_id))
        selection = await self._router.resolve(
            campaign_id,
            ModelRole.NARRATION_VALIDATOR,
        )
        if selection is None:
            return await original_publish_fallback(self, *args, **kwargs)

        published, publication = NarrationPublicationGuard.publish(authority, "", None)
        validator = TurnAuthorityValidator(self._router)
        try:
            result = await validator.validate(selection, authority, published)
        except NarrationValidationError:
            return await original_publish_fallback(self, *args, **kwargs)

        attempt_index = int(kwargs.get("attempt_index") or 0)
        await audit.record_attempt(
            run,
            attempt_index=attempt_index,
            candidate_text=published,
            result=result,
            telemetry={
                **validator.telemetry,
                "authority_version": authority.version,
                "publication_guard": publication,
                "repetition_authority_projection": True,
            },
        )
        if result.verdict != "pass":
            fallback_kwargs = dict(kwargs)
            fallback_kwargs["attempt_index"] = attempt_index + 1
            return await original_publish_fallback(self, *args, **fallback_kwargs)

        repair_attempts = int(kwargs.get("repair_attempts") or 0)
        gate = await audit.finalize(
            run,
            status="repaired",
            final_text=published,
            repair_attempts=repair_attempts,
            failure_reason=reason[:2000],
        )
        telemetry = dict(kwargs.get("telemetry") or {})
        return AuthorityNarrationResult(
            text=published,
            telemetry={
                **telemetry,
                "narration_validation": {
                    "status": gate.status,
                    "validation_run_id": str(gate.validation_run_id),
                    "authority_version": authority.version,
                    "publication_guard": publication,
                    "repetition_authority_projection": True,
                    "validator_telemetry": validator.telemetry,
                    "reason": reason[:2000],
                },
            },
            validation_run_id=gate.validation_run_id,
            validation_status=gate.status,
        )

    AuthorityNarrationPipeline._publish_fallback = validated_repetition_fallback


__all__ = ["NarrationRepetitionGuard", "RepetitionMatch", "install"]
