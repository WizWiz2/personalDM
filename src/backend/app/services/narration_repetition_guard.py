from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.turn_repo import TurnRepository
from app.models.turn import ChatMessage
from app.models.turn_authority import TurnAuthority

_WORD_RE = re.compile(r"[\w]+", re.UNICODE)


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


__all__ = ["NarrationRepetitionGuard", "RepetitionMatch"]
