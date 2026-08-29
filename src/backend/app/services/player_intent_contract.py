from __future__ import annotations

import re


_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_CYRILLIC_RE = re.compile(r"[а-яё]", re.IGNORECASE)
_LATIN_RE = re.compile(r"[a-z]", re.IGNORECASE)


def expects_russian(text: str) -> bool:
    """Return whether the surface is clearly Russian enough for a formal language check."""
    return len(_CYRILLIC_RE.findall(text or "")) >= 3


def contains_cjk(text: object) -> bool:
    """Detect a script-level drift; this is orthographic, not semantic classification."""
    return bool(_CJK_RE.search(str(text or "")))


def language_mismatch(text: str, player_input: str) -> bool:
    """Reject obvious language drift while tolerating Latin canonical names in Russian prose."""
    if not expects_russian(player_input):
        return False
    if contains_cjk(text):
        return True
    cyrillic = len(_CYRILLIC_RE.findall(text or ""))
    latin = len(_LATIN_RE.findall(text or ""))
    letters = cyrillic + latin
    return letters >= 24 and latin >= 16 and latin > cyrillic * 1.25


# The helpers below intentionally remain as compatibility entry points while semantic ownership is
# migrated to Planner/Validator. They contain no vocabulary, stem or regex-based meaning inference.
# Runtime callers must use typed model output for these decisions.
def has_unresolved_choice(text: str) -> bool:
    del text
    return False


def intent_corresponds(player_input: str, planned_intent: str) -> bool:
    del player_input, planned_intent
    return True


def unresolved_player_completion(
    candidate: str,
    *,
    player_input: str,
    player_name: str | None,
) -> bool:
    del candidate, player_input, player_name
    return False


def unauthorized_player_speech(
    candidate: str,
    *,
    player_input: str,
    player_name: str | None,
) -> bool:
    del candidate, player_input, player_name
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
