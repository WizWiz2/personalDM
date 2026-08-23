from __future__ import annotations

import re

from app.models.turn_authority import PlannedNpcIntroduction
from app.services.location_identity import same_location_reference

_INSTALLED = False

# Keep this vocabulary bounded to the generic contact roles already supported by
# TurnAuthorityPlanner. These are temporary identities, not invented proper names.
_GENERIC_CONTACTS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (re.compile(r"\bинформатор\w*\b", re.IGNORECASE), "Информатор", "информатор"),
    (re.compile(r"\bсвидетел\w*\b", re.IGNORECASE), "Свидетель", "свидетель"),
    (re.compile(r"\bпродавц\w*\b", re.IGNORECASE), "Продавец", "продавец"),
    (re.compile(r"\bбармен\w*\b", re.IGNORECASE), "Бармен", "бармен"),
    (re.compile(r"\bтрактирщ\w*\b", re.IGNORECASE), "Трактирщик", "трактирщик"),
    (re.compile(r"\bхозя\w*\b", re.IGNORECASE), "Хозяин", "хозяин"),
    (re.compile(r"\bохран\w*\b", re.IGNORECASE), "Охранник", "охранник"),
    (re.compile(r"\bдежур\w*\b", re.IGNORECASE), "Дежурный", "дежурный"),
    (re.compile(r"\bжил\w*\b", re.IGNORECASE), "Жилец", "жилец"),
    (re.compile(r"\bслужащ\w*\b", re.IGNORECASE), "Служащий", "служащий"),
    (re.compile(r"\bклерк\w*\b", re.IGNORECASE), "Клерк", "клерк"),
    (re.compile(r"\bпрохож\w*\b", re.IGNORECASE), "Прохожий", "прохожий"),
    (re.compile(r"\binformant\b", re.IGNORECASE), "Informant", "informant"),
    (re.compile(r"\bwitness\b", re.IGNORECASE), "Witness", "witness"),
    (re.compile(r"\bclerk\b", re.IGNORECASE), "Clerk", "clerk"),
    (re.compile(r"\bbartender\b", re.IGNORECASE), "Bartender", "bartender"),
    (re.compile(r"\binnkeeper\b", re.IGNORECASE), "Innkeeper", "innkeeper"),
    (re.compile(r"\bguard\b", re.IGNORECASE), "Guard", "guard"),
    (re.compile(r"\bresident\b", re.IGNORECASE), "Resident", "resident"),
    (re.compile(r"\bseller\b", re.IGNORECASE), "Seller", "seller"),
    (re.compile(r"\bpasser[- ]?by\b", re.IGNORECASE), "Passer-by", "passer-by"),
)


def unique_equivalent_location(locations, name: str, exact_matcher):
    """Resolve harmless reference variation without turning identity into fuzzy search.

    Exact canonical/alias equality stays strongest. If it misses (Round 34 differed only by a
    trailing period), compare the already-known campaign locations with the canonical
    location-reference key and accept only one unique equivalent candidate. Duplicate equivalent
    identities remain unresolved/fail-closed.
    """
    exact = exact_matcher(locations, name)
    if exact is not None:
        return exact

    matches = []
    for location in locations:
        candidates = [location.canonical_name, *location.aliases]
        if any(
            same_location_reference(name, candidate)
            for candidate in candidates
            if candidate
        ):
            matches.append(location)
    return matches[0] if len(matches) == 1 else None


def _contact_role(player_input: str) -> tuple[str, str] | None:
    text = " ".join((player_input or "").split())
    for pattern, canonical_name, role in _GENERIC_CONTACTS:
        if pattern.search(text):
            return canonical_name, role
    return None


def _negative_contact_outcome(plan, planner_cls) -> bool:
    consequences = " ".join(plan.observable_consequences).casefold()
    return bool(
        consequences
        and planner_cls._matches_any(  # noqa: SLF001 - contract owner helper
            planner_cls.NEGATIVE_CONTACT_OUTCOME_PATTERNS,
            consequences,
        )
    )


def normalize_affirmative_direct_contact(plan, player_input: str, planner_cls):
    """Preserve an affirmative generic-contact decision when Planner omitted typed identity.

    The planner contract already requires a binary result: positive responder => typed
    npc_introductions; explicit no-contact => no introduction. Round 34 showed a third accidental
    state: Planner emitted an auto-success interaction and prose for a concrete passer-by, but left
    npc_introductions empty; neural repair then changed the outcome to "nobody stops".

    We normalize only that internally affirmative state. We do NOT force contact success when the
    Planner supplied no positive observable consequence, explicitly resolved no contact, or did not
    auto-complete a contact interaction.
    """
    if plan.npc_introductions:
        return plan

    text = " ".join((player_input or "").split()).casefold()
    if not planner_cls._matches_any(planner_cls.CONTACT_INTENT_PATTERNS, text):  # noqa: SLF001
        return plan
    if _negative_contact_outcome(plan, planner_cls):
        return plan
    if not plan.observable_consequences:
        return plan

    role = _contact_role(player_input)
    if role is None:
        return plan

    contact_steps = [
        step
        for step in plan.action_sequence.steps
        if step.resolution == "auto_success"
        and step.action_type in {"interaction", "service"}
        and _contact_role(step.intent or player_input) is not None
    ]
    if not contact_steps:
        return plan

    canonical_name, role_name = role
    plan.npc_introductions.append(
        PlannedNpcIntroduction(
            canonical_name=canonical_name,
            role=role_name,
            temporary_name=True,
            reason=(
                "Игрок прямо инициировал контакт с неизвестным персонажем, а Planner уже "
                "разрешил взаимодействие как auto_success и описал ответ."
            ),
        )
    )

    # Executed sequences, not top-level planner prose, own publication authority. Preserve the
    # planner's already-authored affirmative world/NPC response by binding it to the completed
    # structured contact step instead of letting it disappear during authority projection.
    if not contact_steps[0].observable_outcome:
        contact_steps[0].observable_outcome = plan.observable_consequences[0]
    return plan


def install() -> None:
    """Install the two deterministic Round-34 live-playtest boundary repairs."""
    global _INSTALLED
    if _INSTALLED:
        return

    from app.services.player_destination_authorization import PlayerDestinationAuthorizer
    from app.services.turn_authority_planner import TurnAuthorityPlanner

    original_match_location = PlayerDestinationAuthorizer._match_location
    original_contract_issues = TurnAuthorityPlanner.contract_issues

    def punctuation_tolerant_match(locations, name):
        return unique_equivalent_location(locations, name, original_match_location)

    @classmethod
    def affirmative_contact_contract(cls, plan, player_input):
        normalize_affirmative_direct_contact(plan, player_input, cls)
        return list(original_contract_issues(plan, player_input))

    PlayerDestinationAuthorizer._match_location = staticmethod(punctuation_tolerant_match)
    TurnAuthorityPlanner.contract_issues = affirmative_contact_contract
    _INSTALLED = True


__all__ = [
    "install",
    "normalize_affirmative_direct_contact",
    "unique_equivalent_location",
]
