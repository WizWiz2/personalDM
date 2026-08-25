from __future__ import annotations

from collections.abc import Iterable

from app.models.session_zero_interview import SessionZeroStarterNPC


def _key(value: object) -> str:
    return " ".join(str(value or "").casefold().replace("ё", "е").split())


def names_are_same_identity(left: object, right: object) -> bool:
    """Treat a role-name as the same person as a more specific name with that role as prefix.

    ``Хозяин`` and ``Хозяин трактира`` are one starter. Two different explicit names are not.
    """
    left_key = _key(left)
    right_key = _key(right)
    if not left_key or not right_key:
        return False
    if left_key == right_key:
        return True
    left_tokens = left_key.split()
    right_tokens = right_key.split()
    shorter, longer = (
        (left_tokens, right_tokens)
        if len(left_tokens) <= len(right_tokens)
        else (right_tokens, left_tokens)
    )
    return bool(shorter) and longer[: len(shorter)] == shorter


def _merge_starter_spec(
    established: SessionZeroStarterNPC,
    incoming: SessionZeroStarterNPC,
) -> SessionZeroStarterNPC:
    """Refine one starter identity without silently rewriting established details."""
    payload = established.model_dump(mode="python")
    established_name = _key(established.name)
    incoming_name = _key(incoming.name)
    if incoming_name and (
        not established_name or len(incoming_name) > len(established_name)
    ):
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

    Session Zero tool calls are patches. The control model may alternate a role placeholder and a
    later explicit name for the same physically-present witness. Reconciliation keeps that identity
    deterministic without merging two different explicitly named people.
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
            if name_key and names_are_same_identity(existing.name, spec.name)
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
                if _key(existing.role) == role_key and _key(existing.name) == only_name
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


def sanitize_existing_present_npc_introductions(plan, present_names: Iterable[str]):
    """Drop introductions that exactly name a character already physically present.

    This is not permission for a genuinely new NPC: unsolicited introductions remain in the plan
    and are still rejected by the fail-closed authority contract.
    """
    known = [value for value in present_names if _key(value)]
    if not known or not getattr(plan, "npc_introductions", None):
        return plan
    plan.npc_introductions[:] = [
        intro
        for intro in plan.npc_introductions
        if not any(
            names_are_same_identity(getattr(intro, "canonical_name", None), present)
            for present in known
        )
    ]
    return plan


__all__ = [
    "names_are_same_identity",
    "present_character_names",
    "reconcile_starter_npcs",
    "sanitize_existing_present_npc_introductions",
]
