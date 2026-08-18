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

# ``player_intent`` is a model-authored semantic summary, not an exact echo. These coarse modes let
# the deterministic hand-off distinguish a legitimate paraphrase from a stale plan without requiring
# Russian words to share the same stem. They intentionally describe player-controlled action classes,
# not story topics or transcript-specific entities.
_INTENT_MODE_PATTERNS: dict[str, tuple[str, ...]] = {
    "movement": (
        r"\b(?:иду|идём|идем|идти|пойду|пойти|еду|ехать|поеду|поехать|выхожу|выйти|"
        r"ухожу|уйти|покида\w*|направля\w*|отправля\w*|возвраща\w*|перехо\w*|"
        r"перейти|вхо\w*|войти|захо\w*|зайти|прихо\w*|прийти|добер\w*|добрат\w*|"
        r"дой\w*|дойти|перемест\w*)\b",
        r"\b(?:go|going|leave|leaving|head|heading|return|returning|enter|entering|"
        r"travel|travelling|traveling|move|moving|reach|arrive|arriving)\b",
    ),
    "observation": (
        r"\b(?:осматр\w*|рассматр\w*|обыск\w*|ищ\w*|изуч\w*|исслед\w*|провер\w*|"
        r"наблюд\w*|разглядыва\w*|прочес\w*|оцен\w*|заглян\w*|взглян\w*)\b",
        r"\b(?:inspect|inspecting|examine|examining|search|searching|investigate|"
        r"investigating|study|studying|check|checking|observe|observing|assess|assessing|"
        r"look|looking|peek|peeking)\b",
    ),
    "dialogue": (
        r"\b(?:спраш\w*|спрос\w*|уточн\w*|выясн\w*|узна\w*|расспраш\w*|говор\w*|"
        r"скаж\w*|расскаж\w*|объясн\w*|ответ\w*|обращ\w*|поговор\w*|попрос\w*|"
        r"получ\w*\s+(?:информац\w*|сведен\w*|ответ\w*))\b",
        r"\b(?:ask|asking|question|questioning|clarify|clarifying|learn|learning|"
        r"tell|telling|say|saying|speak|speaking|talk|talking|answer|reply)\b",
    ),
    "interaction": (
        r"\b(?:(?:при)?откр\w*|закр\w*|(?:по)?стуч\w*|звон\w*|нажима\w*|использ\w*|"
        r"включа\w*|выключа\w*|трога\w*|двига\w*)\b",
        r"\b(?:open|opening|close|closing|knock|knocking|ring|ringing|press|pressing|"
        r"use|using|touch|touching)\b",
    ),
    "inventory": (
        r"\b(?:беру|взять|кладу|полож\w*|убира\w*|достаю|достать|переда\w*|отда\w*)\b",
        r"\b(?:take|taking|pick|picking|put|placing|store|storing|give|giving|hand|handing)\b",
    ),
    "wait": (
        r"\b(?:жду|ждать|ожида\w*)\b",
        r"\b(?:wait|waiting)\b",
    ),
    "rest": (
        r"\b(?:сплю|спать|отдыха\w*|ложусь|лечь)\b",
        r"\b(?:sleep|sleeping|rest|resting)\b",
    ),
}
_INTERROGATIVE_RE = re.compile(
    r"(?:\?|^\s*(?:кто|что|где|куда|откуда|когда|почему|зачем|как|сколько|"
    r"who|what|where|when|why|how)\b)",
    flags=re.IGNORECASE,
)
_HIGH_RISK_INTENT_MODES = {"movement", "interaction", "inventory", "wait", "rest"}

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

_QUOTED_TEXT_RE = re.compile(
    r"«([^»]{2,})»|“([^”]{2,})”|\"([^\"]{2,})\"|‘([^’]{2,})’|'([^']{2,})'",
    re.DOTALL,
)


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


def _intent_modes(text: str) -> set[str]:
    normalized = " ".join((text or "").casefold().replace("ё", "е").split())
    modes = {
        mode
        for mode, patterns in _INTENT_MODE_PATTERNS.items()
        if any(re.search(pattern, normalized, flags=re.IGNORECASE) for pattern in patterns)
    }
    if _INTERROGATIVE_RE.search(normalized):
        modes.add("dialogue")
    return modes


def _has_lexical_anchor(source: list[str], target: list[str]) -> bool:
    for left in source:
        for right in target:
            if left == right or left in right or right in left:
                return True
            if SequenceMatcher(None, left, right).ratio() >= 0.56:
                return True
    return False


def intent_corresponds(player_input: str, planned_intent: str) -> bool:
    """Reject clearly stale intent summaries while accepting genuine semantic paraphrases.

    ``player_intent`` is diagnostic text, not player-action authority. Lexical overlap is useful but
    cannot be mandatory: a control model may validly summarize ``Во сколько это было?`` as
    ``Получить сведения о времени``. Safety therefore comes first from recognized action classes and
    the separate structured movement/choice/contact contracts. Opaque semantic paraphrases are not
    rejected merely for choosing different nouns or verbs.
    """
    source = _content_tokens(player_input)
    target = _content_tokens(planned_intent)
    if not source or not target:
        return True
    # Preserve the original contract's tolerance for short diagnostic labels such as "Проверка".
    # There is too little signal to call a one-token summary stale; structured contracts still gate
    # movement, contacts and other stateful actions independently.
    if len(source) < 2 or len(target) < 2:
        return True

    source_modes = _intent_modes(player_input)
    target_modes = _intent_modes(planned_intent)

    # This check deliberately precedes lexical overlap. Sharing a noun such as "door" must not make
    # a stale "knock and enter" plan valid when the current player only asked to inspect that door.
    if (target_modes & _HIGH_RISK_INTENT_MODES) - source_modes:
        return False

    if _has_lexical_anchor(source, target):
        return True

    if source_modes and target_modes:
        return bool(source_modes & target_modes)

    # One side can be an abstract summary that has no recognized action verb ("получить сведения",
    # "понять обстановку"). Do not turn that vocabulary choice into a conservative-fallback storm.
    if source_modes or target_modes:
        return True

    # With neither lexical nor structural signal, retain the old fail-closed behavior for substantial
    # unrelated strings.
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


def _quote_text(match: re.Match[str]) -> str:
    return next((value for value in match.groups() if value is not None), "")


def unauthorized_player_speech(
    candidate: str,
    *,
    player_input: str,
    player_name: str | None,
) -> bool:
    """Detect newly invented quoted protagonist speech using explicit speech attribution.

    Position matters: ``Рэт говорит: «...»`` attributes the quote to the player, while
    ``«Рэт, решай сам», — говорит Грета`` merely addresses the player and must remain legal.
    Quoted text may contain sentence punctuation, so it is parsed before sentence segmentation.
    """
    if not player_name:
        return False
    player_key = " ".join(player_name.casefold().split())
    input_key = " ".join((player_input or "").casefold().split())
    candidate_key = candidate.casefold()

    for match in _QUOTED_TEXT_RE.finditer(candidate):
        quote = _quote_text(match)
        quote_key = " ".join(quote.casefold().split())
        if len(quote_key) < 4 or quote_key in input_key:
            continue

        prefix_start = max(0, match.start() - 180)
        prefix = candidate_key[prefix_start : match.start()]
        player_index = prefix.rfind(player_key)
        if player_index < 0:
            continue
        attribution = prefix[player_index:]
        if any(stem in attribution for stem in _PLAYER_SPEECH_STEMS):
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
