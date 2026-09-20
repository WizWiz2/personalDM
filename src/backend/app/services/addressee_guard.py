from __future__ import annotations

import re

_PLURAL_IMPERATIVE = re.compile(r"(?iu)\b([а-яё]{4,})(?:йтесь|йте|итесь)\b")
_LOOK = re.compile(
    r"(?iu)(опиши|подробн|как выгляд|разгляд|их тел|что у них|сорочк)"
)
_PLAYER_UNDRESS = re.compile(r"(?iu)раздев")


def _fold(text: str) -> str:
    return text.replace("ё", "е").replace("Ё", "Е")


def plural_imperative_stems(player_input: str) -> list[str]:
    folded = _fold(player_input)
    return [match.group(1).lower() for match in _PLURAL_IMPERATIVE.finditer(folded)]


def _player_claims(player_input: str, stem: str) -> bool:
    root = _fold(stem)[:5]
    folded = _fold(player_input).lower()
    pattern = r"(?:я|начинаю|продолжаю|снимаю|раздеваю(?:сь)?).{0,16}" + re.escape(root)
    return bool(re.search(pattern, folded))



def _stem_in_intent(stem: str, intent: str) -> bool:
    hints = [stem[:4]]
    if stem.startswith("сним"):
        hints.extend(("снят", "снять", "снял"))
    return any(hint and hint in intent for hint in hints)


def retain_addressed_actions(player_input: str, actions: list) -> list:
    """Drop model actions that perform a plural imperative as the player's own act.

    "Раздевайтесь" / "снимайте" address other people. A frozen intent must not
    turn that into the player undressing or taking an unrelated item.
    """

    stems = plural_imperative_stems(player_input)
    if not stems:
        return list(actions)
    kept = []
    for action in actions:
        intent = _fold(str(getattr(action, "intent", "") or "")).lower()
        hit = next((stem for stem in stems if _stem_in_intent(stem, intent)), None)
        if hit is not None and not _player_claims(player_input, hit):
            continue
        kept.append(action)
    return kept


def is_look_request(player_input: str) -> bool:
    folded = _fold(player_input)
    return bool(_LOOK.search(folded)) and not plural_imperative_stems(player_input)


def scrub_uninvited_spawn(plan, player_input: str) -> None:
    """A request to describe people already present is not a new arrival or a player action.

    Contact-seeking (addressed_response_requested) must keep typed npc_introductions: looking for
    people who are not yet present is an encounter commitment, not a describe-present scrub.
    """

    if not is_look_request(player_input):
        return
    if getattr(plan, "addressed_response_requested", False):
        return
    if getattr(plan, "npc_introductions", None):
        plan.npc_introductions = []
    consequences = getattr(plan, "observable_consequences", None)
    if isinstance(consequences, list):
        plan.observable_consequences = [
            item for item in consequences if not _PLAYER_UNDRESS.search(str(item))
        ]
