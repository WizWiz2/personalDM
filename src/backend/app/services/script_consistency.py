"""One word, one script: a letter of another Unicode script inside a word is a typing defect."""

import unicodedata
from collections.abc import Iterable
from itertools import groupby


def _scripts(word: str) -> list[str]:
    return [unicodedata.name(letter, "").split(" ")[0] for letter in word]


def _words(text: str) -> list[str]:
    return ["".join(chars) for is_word, chars in groupby(text, key=str.isalpha) if is_word]


def consistent_script(text: str, names: Iterable[str] = ()) -> str:
    """Replace a mixed-script word with the one single-script word of the same text or of a cast
    name that it equals letter by letter, the other-script letters aside («Eгор» → «Егор»)."""
    vocabulary = {w.casefold() for w in [*_words(text), *_words(" ".join(names))]
                  if len(set(_scripts(w))) == 1}

    def fixed(word: str) -> str:
        scripts = _scripts(word)
        if len(set(scripts)) < 2:
            return word
        major = max(set(scripts), key=scripts.count)
        matches = [w for w in vocabulary if len(w) == len(word) and _scripts(w)[0] == major and all(
            s != major or a.casefold() == b for a, b, s in zip(word, w, scripts, strict=True))]
        if len(matches) != 1:
            return word
        return "".join(b.upper() if a.isupper() else b for a, b in zip(word, matches[0], strict=True))

    return "".join(fixed(part) if part.isalpha() else part
                   for _, chars in groupby(text or "", key=str.isalpha) for part in ["".join(chars)])
