from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Literal

ActorRole = Literal["speaker", "addressee"]
InformationRecipient = Literal["narrator", "character"]


class LinguisticParserUnavailable(RuntimeError):
    """Raised when the production Russian dependency parser is not installed."""


@dataclass(frozen=True)
class LinguisticIntentAnalysis:
    """High-confidence facts derived from Universal Dependencies annotations.

    ``None`` means the syntax is ambiguous and the semantic model remains authoritative.  The
    parser deliberately does not guess from surface forms.
    """

    uniform_action_role: ActorRole | None = None
    action_roles: tuple[ActorRole | None, ...] = ()
    imperative_clauses: tuple[str, ...] = ()
    information_request_only: bool | None = None
    information_recipient: InformationRecipient | None = None


@lru_cache(maxsize=1)
def _russian_pipeline():
    try:
        import spacy

        return spacy.load("ru_core_news_sm")
    except (ImportError, OSError) as exc:
        raise LinguisticParserUnavailable(
            "Russian syntax model ru_core_news_sm is unavailable; reinstall backend dependencies"
        ) from exc


@lru_cache(maxsize=1)
def _russian_morphology():
    try:
        import pymorphy3

        return pymorphy3.MorphAnalyzer()
    except (ImportError, OSError) as exc:
        raise LinguisticParserUnavailable(
            "Russian morphological dictionary is unavailable; reinstall backend dependencies"
        ) from exc


@lru_cache(maxsize=2048)
def _dictionary_role(word: str) -> ActorRole | None:
    """Resolve only an unambiguous finite-verb form, with UD providing its context/POS.

    The statistical tagger can label an imperative as first person. A grammatical
    dictionary corrects that error without a vocabulary of application commands.
    Ambiguous forms leave the contextual/semantic analysis authoritative.
    """
    analyses = [entry for entry in _russian_morphology().parse(word) if entry.tag.POS == "VERB"]
    roles = {"addressee" if entry.tag.mood == "impr" else "speaker"
             if entry.tag.person == "1per" else None for entry in analyses}
    return next(iter(roles)) if len(roles) == 1 and None not in roles else None


class LinguisticIntentAnalyzer:
    """Extract grammatical ownership from UD context and unambiguous morphology."""

    def __init__(self, pipeline: Any | None = None):
        self._pipeline = pipeline

    @property
    def pipeline(self):
        return self._pipeline or _russian_pipeline()

    @staticmethod
    def _values(token: Any, feature: str) -> set[str]:
        return set(token.morph.get(feature))

    @classmethod
    def _predicate_roles(cls, doc: Any) -> set[ActorRole]:
        roles: set[ActorRole] = set()
        for token in doc:
            if token.pos_ not in {"VERB", "AUX"}:
                continue
            dictionary_role = _dictionary_role(token.text)
            if dictionary_role is not None:
                roles.add(dictionary_role)
                continue
            if "Imp" in cls._values(token, "Mood"):
                roles.add("addressee")
            if cls._values(token, "Person").intersection({"1", "First"}):
                roles.add("speaker")
        return roles

    @classmethod
    def _evidence_roles(cls, doc: Any, source: str, evidence: str) -> set[ActorRole]:
        """Read evidence tokens from the complete utterance so morphology keeps its context."""

        roles: set[ActorRole] = set()
        if not evidence:
            return roles
        offset = 0
        while (start := source.find(evidence, offset)) >= 0:
            end = start + len(evidence)
            span = doc.char_span(start, end, alignment_mode="expand")
            if span is not None:
                roles.update(cls._predicate_roles(span))
            offset = end
        return roles

    @classmethod
    def _direct_character_address(cls, doc: Any) -> bool:
        for token in doc:
            if token.dep_ == "vocative":
                return True
            if token.pos_ in {"VERB", "AUX", "PRON"} and cls._values(
                token, "Person"
            ).intersection({"2", "Second"}):
                return True
        return False

    @staticmethod
    def _is_interrogative_sentence(sentence: Any) -> bool:
        # This is syntax punctuation supplied by the user, not a vocabulary heuristic.
        return any(token.is_punct and token.text == "?" for token in sentence)

    @staticmethod
    def _name_lemmas(token: Any) -> set[str]:
        return {token.lemma_} | {
            child.lemma_ for child in token.children if child.dep_ in {"flat", "flat:name", "appos"}
        }

    def subject_is(self, prose: str, evidence: str, name: str) -> bool:
        """Whether a predicate inside ``evidence`` has the named person as its grammatical subject.

        A pronoun subject, or a predicate with no expressed subject, takes the last nominal
        subject seen from the sentence before the span onward.
        """
        doc = self.pipeline(prose)
        start = prose.find(evidence)
        span = doc.char_span(start, start + len(evidence), alignment_mode="expand")
        if start < 0 or span is None:
            return False
        owner = self._name_lemmas(next(iter(self.pipeline(name).sents)).root)
        first = span[0].sent.start
        subject: set[str] = set()
        for token in doc[doc[first - 1].sent.start if first else first : span.end]:
            if token.pos_ not in {"VERB", "AUX"} or token.dep_ not in {"ROOT", "conj"}:
                continue
            nominal = next((c for c in token.children if c.dep_.startswith("nsubj")), None)
            if nominal is not None and nominal.pos_ != "PRON":
                subject = self._name_lemmas(nominal)
            if token.i >= span.start and subject & owner:
                return True
        return False

    def stated_acts(self, player_input: str) -> tuple[str, ...]:
        """Clauses of the speaker's own finite predicates outside quoted speech: each stated act the
        intent must map to an entry (REPORT14 p10 «иду обратно на пристань»)."""
        doc = self.pipeline(player_input)
        quoted, depth = set(), 0
        for token in doc:
            if token.is_quote:
                depth = (0 if depth else 1) if token.text in {'"', "'"} else \
                    depth + 1 if token.is_left_punct else max(0, depth - 1)
            elif depth:
                quoted.add(token.i)
        predicates = {token.i for token in doc if token.pos_ in {"VERB", "AUX"}
                      and token.dep_ in {"ROOT", "conj"} and token.i not in quoted
                      and self._values(token, "Person").intersection({"1", "First"})}

        def owner(token):
            while token.i not in predicates and token.head.i != token.i:
                token = token.head
            return token.i if token.i in predicates else None

        clauses: dict[int, list] = {}
        for token in doc:
            if (key := owner(token)) is not None:
                clauses.setdefault(key, []).append(token)
        return tuple(player_input[tokens[0].idx: tokens[-1].idx + len(tokens[-1])]
                     for tokens in clauses.values())

    def analyze(
        self,
        player_input: str,
        action_evidence: list[str] | None = None,
    ) -> LinguisticIntentAnalysis:
        doc = self.pipeline(player_input)
        roles = self._predicate_roles(doc)
        uniform_role = next(iter(roles)) if len(roles) == 1 else None
        imperative_clauses = tuple(
            sentence.text.strip()
            for sentence in doc.sents
            if "addressee" in self._predicate_roles(sentence)
        )
        action_roles: tuple[ActorRole | None, ...] = tuple(
            (
                next(iter(evidence_roles))
                if len(evidence_roles := self._evidence_roles(doc, player_input, evidence)) == 1
                else None
            )
            for evidence in (action_evidence or [])
        )

        sentences = [sentence for sentence in doc.sents if sentence.text.strip()]
        all_interrogative = bool(sentences) and all(
            self._is_interrogative_sentence(sentence) for sentence in sentences
        )
        has_imperative = "addressee" in roles
        if all_interrogative and not has_imperative:
            recipient: InformationRecipient = (
                "character" if self._direct_character_address(doc) else "narrator"
            )
            return LinguisticIntentAnalysis(
                uniform_action_role=uniform_role,
                action_roles=action_roles,
                imperative_clauses=imperative_clauses,
                information_request_only=True,
                information_recipient=recipient,
            )

        return LinguisticIntentAnalysis(
            uniform_action_role=uniform_role,
            action_roles=action_roles,
            imperative_clauses=imperative_clauses,
        )


__all__ = [
    "LinguisticIntentAnalysis",
    "LinguisticIntentAnalyzer",
    "LinguisticParserUnavailable",
]
