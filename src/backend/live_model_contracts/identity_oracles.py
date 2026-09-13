from __future__ import annotations

import re

_PLACEHOLDER_FRAGMENTS = (
    "безымян",
    "неизвестн",
    "незнаком",
    "дежурн",
    "сотрудник",
    "служащ",
    "человек",
    "мужчина",
    "женщина",
    "npc",
)
_WORD_RE = re.compile(r"[\w-]+", flags=re.UNICODE)


def _fold(value: object) -> str:
    return " ".join(str(value or "").casefold().replace("ё", "е").split())


def _tokens(value: object) -> list[str]:
    return _WORD_RE.findall(_fold(value))


def _explicit_surface_name(name: str, content: str) -> bool:
    """Require the stable canonical name to be visibly grounded in published assistant text."""
    wanted = _tokens(name)
    surface = _tokens(content)
    if not wanted or len(surface) < len(wanted):
        return False
    width = len(wanted)
    return any(surface[index : index + width] == wanted for index in range(len(surface) - width + 1))


def name_binding_failures(entity: dict, turns: list[dict]) -> list[str]:
    """Validate that a promoted personal identity was actually established by the DM surface.

    This oracle deliberately does not infer names from user prompts or from model metadata. A stable
    canonical identity is acceptable only when an active published assistant turn itself contains the
    same name. That catches the regression where an unsupported model-invented personal name was
    persisted while still allowing a temporary role NPC to become stable after an explicit reveal.
    """

    failures: list[str] = []
    name = str(entity.get("name") or "").strip()
    folded_name = _fold(name)
    if not name:
        return [f"promoted NPC has no canonical name: {entity}"]
    if any(fragment in folded_name for fragment in _PLACEHOLDER_FRAGMENTS):
        failures.append(f"promoted NPC still has placeholder identity: {entity}")

    active_assistant = [
        str(turn.get("content") or "")
        for turn in turns
        if turn.get("role") == "assistant" and turn.get("status") == "active"
    ]
    if not any(_explicit_surface_name(name, content) for content in active_assistant):
        failures.append(
            f"stable NPC name {name!r} is not grounded in any active published assistant turn"
        )

    return failures


__all__ = ["name_binding_failures"]
