"""Structural narrator contracts: speakers, presence vs solitude, intro identity names.

These helpers encode machine-checkable authority/identity rules. They do not invent story
semantics from keyword game logic: speaker allowlists come from typed cast/intros, solitude
rejection compares authorized cast size to empty-of-people claims, and intro naming delegates description-as-identity checks to name_identity_contract.
"""

from __future__ import annotations

import re

from app.services.entity_identity import identity_key
from app.services.name_identity_contract import (
    description_used_as_identity_name,
    extract_leading_short_designation,
    identity_display_label,
    is_usable_short_designation,
    repair_introduction_identity,
    repair_persisted_character_identity,
)

# Discourse frames that attribute NEW spoken lines to the second-person protagonist.
# Structural attribution only — not a plot/emotion lexicon.
_PROTAGONIST_SPEECH_FRAME_RE = re.compile(
    r"(?:"
    r"твой\s+голос|"
    r"ты\s+(?:говоришь|спрашиваешь|отвечаешь|произносишь|шепчешь|кричишь|"
    r"добавляешь|замечаешь|произн[её]с|сказал[аи]?|спросил[аи]?|ответил[аи]?)|"
    r"(?:говоришь|спрашиваешь|отвечаешь|сказал[аи]?|спросил[аи]?)\s+ты|"
    r"[—\-]\s*(?:сказал[аи]?|спросил[аи]?|ответил[аи]?)\s+ты\b"
    r")",
    flags=re.IGNORECASE,
)

_QUOTE_RE = re.compile(r"«([^»]{1,1600})»|“([^”]{1,1600})”|\"([^\"]{1,1600})\"")

# Empty-of-people / solitude claims (cast contradiction), not furniture emptiness.
_SOLITUDE_CLAIM_RE = re.compile(
    r"(?:"
    r"никого\s+нет|"
    r"никого\s+кроме\s+(?:нас|тебя|меня)|"
    r"кроме\s+нас\s+никого|"
    r"здесь\s+(?:никого|пусто\s+от\s+людей)|"
    r"только\s+мы(?:\s|\.|,|!|\?|$)|"
    r"только\s+ты\s+и\s+я|"
    r"совсем\s+один[ао]?|"
    r"в\s+одиночестве|"
    r"пустынн\w*\s+(?:комнат|зал|коридор|дворец|дом)|"
    r"безлюдн"
    r")",
    flags=re.IGNORECASE,
)


def _compact(value: object) -> str:
    return " ".join(str(value or "").split())


def allowed_speakers_from_authority(authority) -> list[str]:
    """Non-player present cast plus typed introductions/arrivals — never the player character."""
    player_key = (
        identity_key(authority.player_character_name)
        if getattr(authority, "player_character_name", None)
        else None
    )
    ordered: list[str] = []
    seen: set[str] = set()
    for name in [
        *list(getattr(authority, "present_character_names", None) or []),
        *list(getattr(authority, "allowed_new_npc_names", None) or []),
        *list(getattr(authority, "allowed_existing_npc_arrival_names", None) or []),
    ]:
        text = _compact(name)
        if not text:
            continue
        key = identity_key(text)
        if not key or key in seen:
            continue
        if player_key and key == player_key:
            continue
        seen.add(key)
        ordered.append(text)
    return ordered


def authorized_physical_cast_names(authority) -> list[str]:
    """Unique authorized physical people for this turn (player included when named)."""
    ordered: list[str] = []
    seen: set[str] = set()
    for name in [
        *list(getattr(authority, "present_character_names", None) or []),
        *list(getattr(authority, "allowed_new_npc_names", None) or []),
        *list(getattr(authority, "allowed_existing_npc_arrival_names", None) or []),
    ]:
        text = _compact(name)
        if not text:
            continue
        key = identity_key(text)
        if not key or key in seen:
            continue
        seen.add(key)
        ordered.append(text)
    return ordered


def authorized_nonplayer_count(authority) -> int:
    return len(allowed_speakers_from_authority(authority))


def presence_vs_solitude_constraint(authority) -> str | None:
    """Hard canon line when typed intros/arrivals/non-player cast make solitude false."""
    speakers = allowed_speakers_from_authority(authority)
    if not speakers:
        return None
    named = ", ".join(speakers)
    return (
        "[PRESENCE CANON] Authorized people are physically present this turn: "
        f"{named}. Claiming the place is empty of people, that nobody is here, or "
        "'only us'/solitude against that cast is forbidden."
    )
def protagonist_speech_violation_spans(candidate: str, authority) -> list[str]:
    """Spans where narrator attributes new dialogue to the hero/player.

    Second-person speech performance is never on allowed_speakers. Quotes that merely
    echo player_input as performed speech are also unauthorized.
    """
    text = candidate or ""
    if not text.strip():
        return []
    player_input = _compact(getattr(authority, "player_input", None))
    player_key = identity_key(player_input)
    spans: list[str] = []
    seen: set[str] = set()

    def add(span: str) -> None:
        value = _compact(span)
        key = identity_key(value)
        if not value or not key or key in seen:
            return
        seen.add(key)
        spans.append(value)

    for match in _PROTAGONIST_SPEECH_FRAME_RE.finditer(text):
        start = max(0, match.start() - 12)
        end = min(len(text), match.end() + 160)
        window = text[start:end]
        add(window if len(window) <= 220 else match.group(0))

    if player_key:
        for match in _QUOTE_RE.finditer(text):
            quote = next((group for group in match.groups() if group is not None), "")
            quote = _compact(quote)
            if not quote:
                continue
            quote_key = identity_key(quote)
            # Re-performing the player's own words as spoken dialogue.
            if quote_key == player_key or (
                len(quote_key) >= 8 and quote_key in player_key
            ) or (
                len(player_key) >= 8 and player_key in quote_key
            ):
                left = max(0, match.start() - 48)
                right = min(len(text), match.end() + 24)
                neighborhood = text[left:right]
                if _PROTAGONIST_SPEECH_FRAME_RE.search(neighborhood) or re.search(
                    r"\bты\b", neighborhood, flags=re.IGNORECASE
                ):
                    add(neighborhood if len(neighborhood) <= 220 else quote)

    return spans


def solitude_claim_violation_spans(candidate: str, authority) -> list[str]:
    """When physical cast has player + ≥1 authorized other, solitude claims fail."""
    if authorized_nonplayer_count(authority) < 1:
        return []
    cast = authorized_physical_cast_names(authority)
    if len(cast) < 2 and authorized_nonplayer_count(authority) < 1:
        return []
    # Cast size: player (optional) + non-player speakers. Non-empty intros alone forbid solitude.
    text = candidate or ""
    spans: list[str] = []
    seen: set[str] = set()
    for match in _SOLITUDE_CLAIM_RE.finditer(text):
        value = _compact(match.group(0))
        key = identity_key(value)
        if not key or key in seen:
            continue
        seen.add(key)
        spans.append(value)
    return spans


__all__ = [
    "allowed_speakers_from_authority",
    "authorized_nonplayer_count",
    "authorized_physical_cast_names",
    "description_used_as_identity_name",
    "extract_leading_short_designation",
    "identity_display_label",
    "is_usable_short_designation",
    "presence_vs_solitude_constraint",
    "protagonist_speech_violation_spans",
    "repair_introduction_identity",
    "repair_persisted_character_identity",
    "solitude_claim_violation_spans",
]
