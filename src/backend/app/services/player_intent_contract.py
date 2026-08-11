from __future__ import annotations

import re
from difflib import SequenceMatcher


_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_CYRILLIC_RE = re.compile(r"[а-яё]", re.IGNORECASE)
_LATIN_RE = re.compile(r"[a-z]", re.IGNORECASE)
_CONTENT_TOKEN_RE = re.compile(r"[a-zа-яё0-9_]{4,}", re.IGNORECASE)

_UNRESOLVED_CHOICE_PATTERNS = (
    r"\bколебл\w*\b",
    r"\bможет\b[^.!?]{0,140}\bможет\b",
    r"\b(?:войти|уйти|остаться|постучать|позвать|идти|пойти|двинуться|отступить)\b"
    r"[^.!?]{0,120}\b(?:или|либо)\b",
    r"\b(?:или|либо)\b[^.!?]{0,120}\?",
    r"\b(?:hesitat\w*|decid\w*)\b[^.!?]{0,140}\bor\b",
)

_STOPWORDS = {
    "этого",
    "этот",
    "этакий",
    "который",
    "которая",
    "которые",
    "чтобы",
    "сейчас",
    "здесь",
    "тогда",
    "потом",
    "снова",
    "просто",
    "очень",
    "хочу",
    "нужно",
    "может",
    "maybe",
    "there",
    "then",
    "just",
    "want",
}

_PLAYER_COMPLETION_STEMS = (
    "вход",
    "заход",
    "пош",
    "направ",
    "постуч",
    "позв",
    "зов",
    "представ",
    "решил",
    "решает",
    "выбрал",
    "выбира",
    "enter",
    "walk",
    "head",
    "knock",
    "call",
    "hail",
    "introduc",
    "decid",
    "choos",
)

_PLAYER_SPEECH_STEMS = (
    "говор",
    "сказ",
    "спрос",
    "отвеч",
    "произн",
    "добав",
    "шеп",
    "крич",
    "say",
    "said",
    "ask",
    "answer",
    "reply",
    "whisper",
    "shout",
)

_QUOTE_RE = re.compile(r"[«\"“‘']([^»\"”’']{2,})[»\"”’']")


def has_unresolved_choice(text: str) -> bool:
    normalized = " ".join((text or "").casefold().split())
    return any(re.search(pattern, normalized, flags=re.IGNORECASE) for pattern in _UNRESOLVED_CHOICE_PATTERNS)


def expects_russian(text: str) -> bool:
    return len(_CYRILLIC_RE.findall(text or "")) >= 3


def contains_cjk(text: object) -> bool:
    return bool(_CJK_RE.search(str(text or "")))


def language_mismatch(text: str, player_input: str) -> bool:
    """Reject obvious language drift while tolerating Latin canonical names inside Russian prose."""
    if not expects_russian(player_input):
        return False
    if contains_cjk(text):
        return True
    cyrillic = len(_CYRILLIC_RE.findall(text or ""))
    latin = len(_LATIN_RE.findall(text or ""))
    letters = cyrillic + latin
    return letters >= 24 and latin >= 16 and latin > cyrillic * 1.25


def _content_tokens(text: str) -> list[str]:
    return [
        token
        for token in _CONTENT_TOKEN_RE.findall((text or "").casefold())
        if token not in _STOPWORDS
    ]


def intent_corresponds(player_input: str, planned_intent: str) -> bool:
    """Catch clearly stale plans without requiring the planner to paraphrase lexically.

    Russian inflection and aspect can substantially change a verb (``стучу`` -> ``постучать``).
    This gate is intentionally conservative: it only rejects plans with no plausible lexical anchor
    to the current input. Strong semantic judging remains the Planner's job, while this boundary
    catches the Round-9 class where a door-opening plan answered an unrelated heraldry input.
    """
    source = _content_tokens(player_input)
    target = _content_tokens(planned_intent)
    if len(source) < 2 or len(target) < 2:
        return True
    for left in source:
        for right in target:
            if left == right or left in right or right in left:
                return True
            if SequenceMatcher(None, left, right).ratio() >= 0.56:
                return True
    return False


def unresolved_player_completion(
    candidate: str,
    *,
    player_input: str,
    player_name: str | None,
) -> bool:
    if not has_unresolved_choice(player_input) or not player_name:
        return False
    name = " ".join(player_name.casefold().split())
    for segment in re.split(r"(?<=[.!?…])\s+|[\r\n]+", candidate or ""):
        normalized = segment.casefold()
        if name not in normalized:
            continue
        if any(stem in normalized for stem in _PLAYER_COMPLETION_STEMS):
            return True
    return False


def unauthorized_player_speech(
    candidate: str,
    *,
    player_input: str,
    player_name: str | None,
) -> bool:
    """Detect newly invented quoted protagonist speech while allowing dialogue supplied by the user."""
    if not player_name:
        return False
    player_key = " ".join(player_name.casefold().split())
    input_key = " ".join((player_input or "").casefold().split())
    for segment in re.split(r"(?<=[.!?…])\s+|[\r\n]+", candidate or ""):
        segment_key = " ".join(segment.casefold().split())
        if player_key not in segment_key:
            continue
        if not any(stem in segment_key for stem in _PLAYER_SPEECH_STEMS):
            continue
        quotes = _QUOTE_RE.findall(segment)
        if not quotes:
            continue
        for quote in quotes:
            quote_key = " ".join(quote.casefold().split())
            if len(quote_key) >= 4 and quote_key not in input_key:
                return True
    return False


__all__ = [
    "contains_cjk",
    "expects_russian",
    "has_unresolved_choice",
    "intent_corresponds",
    "language_mismatch",
    "unauthorized_player_speech",
    "unresolved_player_completion",
]
