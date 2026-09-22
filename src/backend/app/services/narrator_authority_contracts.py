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
_MIN_ACTION_TOKENS = 2
_ACTION_TOKEN_COVERAGE = 0.5
# Pronouns / function words dropped from action-overlap (structural, not a verb lexicon).
_ACTION_STOPWORDS = frozenset(
    {
        "ya",
        "ty",
        "on",
        "ona",
        "oni",
        "my",
        "vy",
        "i",
        "a",
        "no",
        "da",
        "ne",
        "ni",
        "zhe",
        "by",
        "li",
        "v",
        "vo",
        "na",
        "s",
        "so",
        "k",
        "ko",
        "u",
        "o",
        "ob",
        "po",
        "ot",
        "do",
        "za",
        "iz",
        "dlya",
        "pro",
        "pri",
        "bez",
        "nad",
        "pod",
        "eto",
        "kak",
        "chto",
        "togda",
        "kogda",
        "uzhe",
        "eshche",
        "tolko",
        "eshchyo",
    }
)
# Closed 3rd-person speech-act tags after PC name (mirrors 2nd-person frames; not a plot lexicon).
_PC_SPEECH_ACT_AFTER_NAME = (
    r"(?:спрашивает|говорит|отвечает|произносит|шепчет|кричит|"
    r"добавляет|замечает|зада[её]т)"
)
_PC_PRONOUN_SPEECH_RE = re.compile(
    r"(?:"
    r"\bон\b\s+(?:зада[её]т\s+вопрос|спрашивает|говорит|отвечает|произносит)|"
    r"когда\s+он\s+зада[её]т\s+вопрос"
    r")",
    flags=re.IGNORECASE,
)
_PREPOSITION_BEFORE_RE = re.compile(
    r"(?i)(?:^|[\s,;:—\-–])(?:на|к|ко|с|со|у|от|для|о|об|про|перед|за|под|над|при|"
    r"без|до|из|по|во?|обо|через|между|среди)\s+$"
)
# Morphological 3p finite-verb token (past / present-future), not a verb lexicon.
_PC_FINITE_VERB_TOKEN_RE = re.compile(
    r"(?i)^(?:[а-яё-]*[а-яё]л(?:а|о|и)?(?:сь|ся)?|[а-яё-]*[а-яё](?:ет|ёт|ит|ут|ют|ат|ят)(?:ся)?)$"
)
# Tiny closed copula set — existence/state, not voluntary performance.
_PC_COPULA = frozenset({"был", "была", "было", "были"})

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
    """True when a narrated line near-copies player speech cores (structural overlap; speaker-agnostic)."""
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
    """Spans that violate protagonist-speech / player-input content authority.

    Two separate invariants (do not collapse them):
    1) Attribution frames: second-person speech performance is never on allowed_speakers.
    2) Player-speech-core echo: a quoted or dialogue-shaped line that near-copies
       player_input speech cores is invalid regardless of attributed speaker (hero OR
       NPC). player_input is the only authorized source of the protagonist's voluntary
       speech content; NPCs may answer, refuse, or deflect — they must not perform the
       player's line. allowed_speakers still gates who may speak; echo-of-player-input
       is a separate content invariant over those lines.
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
        # Dialogue-dash lines use the same content invariant (speaker-agnostic).
        for match in _DIALOGUE_LINE_RE.finditer(text):
            line = match.group(1)
            if not _quote_echoes_player_speech(line, speech_cores, player_key):
                continue
            left = max(0, match.start() - 48)
            right = min(len(text), match.end() + 24)
            neighborhood = text[left:right]
            add(neighborhood if len(neighborhood) <= 220 else _compact(line))

    return spans



def _content_tokens(key: str) -> list[str]:
    return [tok for tok in key.split() if tok and tok not in _ACTION_STOPWORDS and len(tok) >= 3]


def _soft_token_match(left: str, right: str) -> bool:
    """Prefix-tolerant token match so conjugated Russian verbs still overlap cores."""
    if left == right:
        return True
    if len(left) < 4 or len(right) < 4:
        return False
    limit = min(len(left), len(right))
    shared = 0
    while shared < limit and left[shared] == right[shared]:
        shared += 1
    return shared >= 4 and shared >= int(0.65 * min(len(left), len(right)))


def _soft_token_overlap(window_key: str, core_key: str) -> bool:
    window_tokens = _content_tokens(window_key)
    core_tokens = _content_tokens(core_key)
    if len(core_tokens) < _MIN_ACTION_TOKENS or not window_tokens:
        return False
    matched = 0
    used: set[int] = set()
    for core_tok in core_tokens:
        for idx, win_tok in enumerate(window_tokens):
            if idx in used:
                continue
            if _soft_token_match(core_tok, win_tok):
                used.add(idx)
                matched += 1
                break
    if matched < _MIN_ACTION_TOKENS:
        return False
    return (matched / len(core_tokens)) >= _ACTION_TOKEN_COVERAGE


def _player_action_cores(player_input: object) -> list[str]:
    """Non-dialogue voluntary-action fragments of player_input for restage overlap."""
    raw = str(player_input or "")
    scrubbed = _QUOTE_RE.sub(" ", raw)
    scrubbed = _DIALOGUE_LINE_RE.sub(" ", scrubbed)
    compact = _compact(scrubbed)
    cores: list[str] = []
    seen: set[str] = set()

    def add_core(value: object) -> None:
        text = _compact(value)
        key = identity_key(text)
        if not text or not key or len(_content_tokens(key)) < _MIN_ACTION_TOKENS:
            return
        if key in seen:
            return
        seen.add(key)
        cores.append(text)

    for match in re.finditer(r"(?i)\bя\s+[^.»!?\n]+", compact):
        add_core(match.group(0))
    add_core(compact)
    return cores


def _pc_name_patterns(pc_name: str) -> re.Pattern[str] | None:
    name = _compact(pc_name)
    if not name:
        return None
    return re.compile(rf"(?<!\w){re.escape(name)}(?!\w)", flags=re.IGNORECASE)


def _window_restages_player(
    window: str,
    *,
    action_cores: list[str],
    speech_cores: list[str],
    pc_name: str,
) -> bool:
    cleaned = _compact(window)
    if pc_name:
        cleaned = re.sub(
            rf"(?i)(?<!\w){re.escape(pc_name)}(?!\w)",
            " ",
            cleaned,
        )
    window_key = identity_key(cleaned)
    if not window_key:
        return False
    for core in (*action_cores, *speech_cores):
        core_key = identity_key(core)
        if core_key and _soft_token_overlap(window_key, core_key):
            return True
    return False


def _window_has_pc_finite_agency(window: str, pc_name: str) -> bool:
    """PC-name subject + nearby 3p finite-verb morphology (no verb lexicon)."""
    cleaned = _compact(window)
    match = re.search(
        rf"(?i)(?<!\w){re.escape(pc_name)}(?!\w)\s+(.+)",
        cleaned,
    )
    if not match:
        return False
    tokens = re.findall(r"[А-Яа-яЁё]+", match.group(1))
    for tok in tokens[:4]:
        folded = tok.lower().replace("ё", "е")
        if folded in _PC_COPULA:
            continue
        if _PC_FINITE_VERB_TOKEN_RE.match(tok):
            return True
    return False


def protagonist_action_restage_violation_spans(candidate: str, authority) -> list[str]:
    """Spans that attribute 3rd-person PC voluntary action/speech outside player_input.

    Invariant (publication gate shared with speech echoes): prose must not attribute
    voluntary action or speech *performance* to the player character by canonical name
    (or a clear 3rd-person PC reference after that name) when that act is not grounded
    in player_input. Covers restage-of-input (core overlap) and invented moves (name +
    3p finite-verb agency with no overlap). Second-person house style and oblique
    PC-name mentions without agency remain allowed. Detection uses morphology near the
    PC name — not a large verb lexicon.
    """
    text = candidate or ""
    pc_name = _compact(getattr(authority, "player_character_name", None))
    if not text.strip() or not pc_name:
        return []
    name_re = _pc_name_patterns(pc_name)
    if name_re is None:
        return []

    player_input = getattr(authority, "player_input", None)
    action_cores = _player_action_cores(player_input)
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

    speech_act_re = re.compile(
        rf"(?i)(?<!\w){re.escape(pc_name)}(?!\w)\s+{_PC_SPEECH_ACT_AFTER_NAME}"
    )
    for match in speech_act_re.finditer(text):
        start = max(0, match.start() - 8)
        end = min(len(text), match.end() + 80)
        add(text[start:end] if end - start <= 220 else match.group(0))

    for match in name_re.finditer(text):
        prefix = text[max(0, match.start() - 24) : match.start()]
        if _PREPOSITION_BEFORE_RE.search(prefix):
            continue
        # Subject-like: name followed by a word (verb/adverb), not punctuation-only.
        after = text[match.end() : match.end() + 1]
        if after and after in {'.', ',', ';', ':', '!', '?', '»', '"', "'", ')'}:
            continue
        start = match.start()
        end = min(len(text), match.end() + 100)
        # Prefer clause boundary when nearby.
        clause = re.search(r"[.!?…\n]", text[match.end() : end])
        if clause:
            end = match.end() + clause.start() + 1
        window = text[start:end]
        restages = _window_restages_player(
            window,
            action_cores=action_cores,
            speech_cores=speech_cores,
            pc_name=pc_name,
        )
        if restages or _window_has_pc_finite_agency(window, pc_name):
            add(window if len(window) <= 220 else _compact(window[:220]))

    # Clear 3rd-person PC reference: pronoun speech-performance after a PC-name mention
    # in the same paragraph, when player_input supplied speech.
    if speech_cores:
        for name_match in name_re.finditer(text):
            para_end = text.find("\n\n", name_match.end())
            if para_end < 0:
                para_end = len(text)
            region = text[name_match.start() : para_end]
            for speech_match in _PC_PRONOUN_SPEECH_RE.finditer(region):
                abs_start = name_match.start() + speech_match.start()
                abs_end = name_match.start() + speech_match.end()
                left = max(name_match.start(), abs_start - 40)
                right = min(len(text), abs_end + 24)
                add(text[left:right] if right - left <= 220 else speech_match.group(0))

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
    "protagonist_action_restage_violation_spans",
    "protagonist_speech_violation_spans",
    "repair_introduction_identity",
    "repair_persisted_character_identity",
    "solitude_claim_violation_spans",
]
