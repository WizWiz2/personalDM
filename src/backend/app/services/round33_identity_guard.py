from __future__ import annotations

from collections.abc import Iterable
from contextvars import ContextVar

from app.models.session_zero_interview import SessionZeroStarterNPC

_INSTALLED = False
_PRESENT_CHARACTER_CONTEXT: ContextVar[frozenset[str]] = ContextVar(
    "round33_present_character_context",
    default=frozenset(),
)


def _key(value: object) -> str:
    return " ".join(str(value or "").casefold().replace("ё", "е").split())


def _merge_starter_spec(
    established: SessionZeroStarterNPC,
    incoming: SessionZeroStarterNPC,
) -> SessionZeroStarterNPC:
    """Refine one starter identity without silently rewriting established details."""
    payload = established.model_dump(mode="python")
    if not _key(established.name) and _key(incoming.name):
        payload["name"] = incoming.name
    for field in ("description", "reason"):
        if not _key(payload.get(field)) and _key(getattr(incoming, field)):
            payload[field] = getattr(incoming, field)
    payload["present_at_start"] = bool(
        established.present_at_start or incoming.present_at_start
    )
    return SessionZeroStarterNPC.model_validate(payload)


def reconcile_starter_npcs(
    specs: Iterable[SessionZeroStarterNPC],
) -> list[SessionZeroStarterNPC]:
    """Collapse repeated role/name representations of one uniquely identified starter.

    Session Zero tool calls are patches. Qwen may alternate `Посетительница` and `Анна` for the
    same physically-present witness. Plain Pydantic equality treats changes in name/description/
    reason as new list entries and bootstrap then materializes duplicate characters.

    Reconciliation stays deterministic:
    * the same explicit name is one identity;
    * one explicit name plus otherwise-unnamed entries with the exact same role is one identity;
    * one named entry may promote one unique earlier role placeholder;
    * two different explicit names with the same role always stay distinct;
    * when a role has multiple named identities, unnamed entries for that role stay separate rather
      than being assigned arbitrarily.
    """
    source = [SessionZeroStarterNPC.model_validate(raw) for raw in specs]
    explicit_names_by_role: dict[str, set[str]] = {}
    for spec in source:
        role_key = _key(spec.role)
        name_key = _key(spec.name)
        if role_key and name_key:
            explicit_names_by_role.setdefault(role_key, set()).add(name_key)

    result: list[SessionZeroStarterNPC] = []
    for spec in source:
        if spec in result:
            continue

        name_key = _key(spec.name)
        role_key = _key(spec.role)
        same_name = [
            index
            for index, existing in enumerate(result)
            if name_key and _key(existing.name) == name_key
        ]
        if len(same_name) == 1:
            target = same_name[0]
            result[target] = _merge_starter_spec(result[target], spec)
            continue

        role_names = explicit_names_by_role.get(role_key, set())
        if len(role_names) == 1:
            only_name = next(iter(role_names))
            named_same_role = [
                index
                for index, existing in enumerate(result)
                if _key(existing.role) == role_key
                and _key(existing.name) == only_name
            ]
            unnamed_same_role = [
                index
                for index, existing in enumerate(result)
                if _key(existing.role) == role_key and not _key(existing.name)
            ]

            if name_key == only_name and len(unnamed_same_role) == 1:
                target = unnamed_same_role[0]
                result[target] = _merge_starter_spec(result[target], spec)
                continue
            if not name_key and len(named_same_role) == 1:
                target = named_same_role[0]
                result[target] = _merge_starter_spec(result[target], spec)
                continue

        result.append(spec)

    return result


def present_character_names(context_messages) -> frozenset[str]:
    """Read exact physical character names from the authoritative scene-state contract."""
    names: set[str] = set()
    prefix = "Physically present characters:"
    for message in context_messages or []:
        content = str(getattr(message, "content", "") or "")
        for line in content.splitlines():
            clean = line.strip()
            if not clean.startswith(prefix):
                continue
            raw = clean[len(prefix) :].strip()
            if not raw or _key(raw) == "player only / none recorded":
                continue
            names.update(part.strip() for part in raw.split(",") if part.strip())
    return frozenset(names)


def sanitize_existing_present_npc_introductions(
    plan,
    present_names: Iterable[str],
):
    """Drop only introductions that exactly name a character already physically present.

    This is not permission for a new NPC. Any genuinely new unsolicited character remains in the
    plan and is still rejected by the existing systemless fail-closed contract. Explicit unknown
    contacts such as a passer-by also remain untouched and keep using the existing contact rules.
    """
    known = {_key(value) for value in present_names if _key(value)}
    if not known or not getattr(plan, "npc_introductions", None):
        return plan
    plan.npc_introductions[:] = [
        intro
        for intro in plan.npc_introductions
        if _key(getattr(intro, "canonical_name", None)) not in known
    ]
    return plan


def install() -> None:
    """Install Round-33 identity reconciliation at existing deterministic boundaries."""
    global _INSTALLED
    if _INSTALLED:
        return

    from app.services.session_zero_agent import (
        SessionZeroInterviewService as BaseSessionZeroInterviewService,
    )
    from app.services.turn_authority_planner import TurnAuthorityPlanner

    original_apply_patch = BaseSessionZeroInterviewService._apply_patch
    original_contract_issues = TurnAuthorityPlanner.contract_issues
    original_plan = TurnAuthorityPlanner.plan

    def reconciled_apply_patch(
        cls,
        previous,
        patch,
        *,
        allowed_topics=None,
        explicit_correction=False,
    ):
        merged = original_apply_patch(
            previous,
            patch,
            allowed_topics=allowed_topics,
            explicit_correction=explicit_correction,
        )
        merged.world.starter_npcs = reconcile_starter_npcs(
            merged.world.starter_npcs
        )
        return merged

    @classmethod
    def present_aware_contract_issues(cls, plan, player_input):
        sanitize_existing_present_npc_introductions(
            plan,
            _PRESENT_CHARACTER_CONTEXT.get(),
        )
        return list(original_contract_issues(plan, player_input))

    async def present_aware_plan(self, selection, context_messages):
        token = _PRESENT_CHARACTER_CONTEXT.set(
            present_character_names(context_messages)
        )
        try:
            return await original_plan(self, selection, context_messages)
        finally:
            _PRESENT_CHARACTER_CONTEXT.reset(token)

    BaseSessionZeroInterviewService._apply_patch = classmethod(reconciled_apply_patch)
    TurnAuthorityPlanner.contract_issues = present_aware_contract_issues
    TurnAuthorityPlanner.plan = present_aware_plan
    _INSTALLED = True


__all__ = [
    "install",
    "present_character_names",
    "reconcile_starter_npcs",
    "sanitize_existing_present_npc_introductions",
]
