"""High-confidence detector for model scratchpad accidentally appended to story prose."""
from __future__ import annotations

import re

_INTERNAL = (
    re.compile(r"(?im)^\s*(?:analysis|reasoning|chain.of.thought|internal note)\s*:\s+"),
    re.compile(r"(?im)^\s*(?:need no invented|end actionable|must avoid inventing)\b"),
    re.compile(r"(?im)^\s*\[/?(?:AUTHORITATIVE SCENE STATE|INTER-AGENT|APPROVED TURN PLAN|SYSTEM PROMPT)\]"),
    re.compile(r"(?im)^\s*<\|(?:im_start|im_end|assistant|analysis|final)\|>"),
    re.compile(r"(?im)^\s*(?:as an ai language model|как языковая модель(?: ии)?)\b"),
)


def leaked_meta_lines(text: str) -> list[str]:
    """Return limited diagnostic evidence; do not delete validated prose."""
    hits = []
    for line in (text or "").splitlines():
        if any(pattern.search(line) for pattern in _INTERNAL):
            hits.append(line.strip()[:100])
        elif re.search(r"(?i)\bneed no invented\b|\bend actionable\b", line):
            hits.append(line.strip()[:100])
        if len(hits) >= 3:
            break
    return hits


__all__ = ["leaked_meta_lines"]
