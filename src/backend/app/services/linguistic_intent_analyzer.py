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


class LinguisticIntentAnalyzer:
    """Extract grammatical ownership from a Russian UD parse, without lexical rules."""

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
