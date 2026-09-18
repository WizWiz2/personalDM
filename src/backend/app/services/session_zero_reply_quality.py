"""Structural checks for Session Zero assistant_message completeness."""

from __future__ import annotations


def assistant_message_incomplete(message: str) -> bool:
    """True when the visible master line stops mid-clause without a sentence end.

    Structural only: terminal punctuation and hanging openers — no word lists.
    """
    text = " ".join((message or "").split())
    if not text:
        return False
    closers = {
        "»",
        '"',
        "'",
        "\u201c",
        "\u201d",
        "\u2018",
        "\u2019",
        ")",
        "）",
        "］",
        "】",
        "〉",
        "》",
    }
    trimmed = text.rstrip()
    while trimmed and trimmed[-1] in closers:
        trimmed = trimmed[:-1].rstrip()
    if not trimmed:
        return True
    if trimmed[-1] in ".!?…":
        return False
    if trimmed[-1] in ",;:—–-(":
        return True
    last = trimmed[-1]
    return last.isalnum() or last == "\u0301"
