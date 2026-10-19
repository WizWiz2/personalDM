"""High-confidence detector for model scratchpad accidentally appended to story prose."""
from __future__ import annotations

import re

_INTERNAL = (
    re.compile(r"(?im)^\s*(?:analysis|reasoning|chain.of.thought|internal note)\s*:\s+"),
    re.compile(r"(?im)^\s*(?:need no invented|end actionable|must avoid inventing)\b"),
    re.compile(r"(?im)^\s*\[/?(?:AUTHORITATIVE SCENE STATE|INTER-AGENT|APPROVED TURN PLAN|SYSTEM PROMPT)\]"),
    re.compile(r"(?im)^\s*<\|(?:im_start|im_end|assistant|analysis|final)\|>"),
    re.compile(r"(?im)^\s*(?:as an ai language model|как языковая модель(?: ии)?)\b"),

    # QA-210 F-24/F-27: a promise of a later answer is not narrative prose.
    re.compile(r"(?i)\b(?:narrator|planner|resolver|validator)(?:-ом)?\b"),
    re.compile(r"(?i)ожидает подачи|перечисление[^\n]{0,100}оста[её]тся за[^\n]{0,50}ответом"),
    # QA-213 T7: a scratchpad aphorism about writing the chronicle
    # is not an event inside the narrative world.
    re.compile(r"(?i)^\\s*хроника не жд[её]т вдохновения[.!?]?\\s*$"),
    # QA-210 F-25: decoder's private-use symbols are not legitimate text.
    re.compile(r"[\uE000-\uF8FF]"),
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
