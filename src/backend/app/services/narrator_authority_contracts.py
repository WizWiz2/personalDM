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
# (a) 2nd-person possessive speech nouns, (b) ты leading into a quote/colon,
# (c) closed speech-act tags with ты. Verb tags stay small; overlap handles echoes.
_PROTAGONIST_SPEECH_FRAME_RE = re.compile(
    r"(?:"
    r"тво(?:й|я|ё|е|и)\s+(?:голос|вопрос|ответ|ш[её]пот|крик|слова|реплик\w*)|"
    r"\bты\b(?=[^\.\n]{0,48}[«\"“:])|"
    r"ты\s+(?:говоришь|спрашиваешь|отвечаешь|произносишь|шепчешь|кричишь|"
    r"добавляешь|замечаешь|произн[её]с|сказал[аи]?|спросил[аи]?|ответил[аи]?)|"
    r"(?:говоришь|спрашиваешь|отвечаешь|сказал[аи]?|спросил[аи]?)\s+ты|"
    r"[—\-]\s*(?:сказал[аи]?|спросил[аи]?|ответил[аи]?)\s+ты\b"
    r")",
    flags=re.IGNORECASE,
)

_QUOTE_RE = re.compile(r"«([^»]{1,1600})»|“([^”]{1,1600})”|\"([^\"]{1,1600})\"")
_DIALOGUE_LINE_RE = re.compile(r"(?m)^[ \t]*[—\-–]\s*(\S.+)$")
_LEADING_FIRST_PERSON_STAGE_RE = re.compile(
    r"^(?:я\s+[^.»!?]+[.»!?]+(?:\s+|$))+",
    flags=re.IGNORECASE,
)
_MIN_ECHO_KEY_LEN = 8
_MIN_ECHO_TOKENS = 3
_ECHO_TOKEN_COVERAGE = 0.8

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


def _player_speech_cores(player_input: object) -> list[str]:
    """Dialogue-shaped fragments of player_input for structural echo checks."""
    raw = str(player_input or "")
    cores: list[str] = []
    seen: set[str] = set()

    def add_core(value: object) -> None:
        text = _compact(value)
        key = identity_key(text)
        if not text or not key or len(key) < _MIN_ECHO_KEY_LEN or key in seen:
            return
        seen.add(key)
        cores.append(text)

    for match in _QUOTE_RE.finditer(raw):
        quote = next((group for group in match.groups() if group is not None), "")
        add_core(quote)
    for match in _DIALOGUE_LINE_RE.finditer(raw):
        add_core(match.group(1))

    compact = _compact(raw)
    add_core(compact)
    if compact:
        stripped = _LEADING_FIRST_PERSON_STAGE_RE.sub("", compact).strip(" -—–")
        add_core(stripped)
    return cores


def _quote_echoes_player_speech(quote: str, cores: list[str], player_key: str) -> bool:
    """True when a narrated quote is a near-copy of player speech (structural overlap)."""
    quote_text = _compact(quote)
    quote_key = identity_key(quote_text)
    if not quote_key or len(quote_key) < _MIN_ECHO_KEY_LEN:
        return False
    if player_key and (
        quote_key == player_key
        or quote_key in player_key
        or (len(player_key) >= _MIN_ECHO_KEY_LEN and player_key in quote_key)
    ):
        return True
    quote_tokens = set(quote_key.split())
    for core in cores:
        core_key = identity_key(core)
        if not core_key:
            continue
        if quote_key == core_key or quote_key in core_key or core_key in quote_key:
            return True
        core_tokens = set(core_key.split())
        if len(quote_tokens) < _MIN_ECHO_TOKENS or not core_tokens:
            continue
        shared = quote_tokens & core_tokens
        if len(shared) >= _MIN_ECHO_TOKENS and (
            len(shared) / len(quote_tokens) >= _ECHO_TOKEN_COVERAGE
        ):
            return True
    return False


def protagonist_speech_violation_spans(candidate: str, authority) -> list[str]:
    """Spans where narrator attributes new dialogue to the hero/player.

    Invariant: protagonist dialogue must not survive publication as narrated performance.
    Second-person speech frames are never on allowed_speakers. Quotes that are a
    structural near-copy of player_input are unauthorized even without a local frame.
    """
    text = candidate or ""
    if not text.strip():
        return []
    player_input = getattr(authority, "player_input", None)
    player_key = identity_key(_compact(player_input))
    speech_cores = _player_speech_cores(player_input)
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

    if player_key or speech_cores:
        for match in _QUOTE_RE.finditer(text):
            quote = next((group for group in match.groups() if group is not None), "")
            if not _quote_echoes_player_speech(quote, speech_cores, player_key):
                continue
            left = max(0, match.start() - 48)
            right = min(len(text), match.end() + 24)
            neighborhood = text[left:right]
            add(neighborhood if len(neighborhood) <= 220 else _compact(quote))

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
