from __future__ import annotations

from collections.abc import Sequence

from fastapi.testclient import TestClient

from live_model_contracts.cases import CaseSpec
from live_model_contracts.snapshot import TruthSnapshot
from live_model_contracts.world import FixtureWorld, add_fact


def _fold(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def _fail(condition: bool, message: str, failures: list[str]) -> None:
    if not condition:
        failures.append(message)


def _active_location(snapshot: TruthSnapshot) -> str | None:
    scene = snapshot.active_scene()
    return str(scene.get("location")) if scene and scene.get("location") else None


def _new_characters(before: TruthSnapshot, after: TruthSnapshot) -> list[dict]:
    before_ids = {row["id"] for row in before.entity_rows(entity_type="character")}
    return [
        row
        for row in after.entity_rows(entity_type="character")
        if row["id"] not in before_ids
    ]


def _no_dead_surface(snapshot: TruthSnapshot, failures: list[str]) -> None:
    surface = _fold(snapshot.assistant_surface)
    forbidden = (
        "пока ничего заметно не меняется",
        "ничего не происходит",
        "пока ничего не меняется",
    )
    _fail(bool(surface), "assistant produced no active published surface", failures)
    _fail(
        not any(value in surface for value in forbidden),
        f"dead-turn surface escaped: {surface!r}",
        failures,
    )


def _contact_in_office(
    before: TruthSnapshot,
    after: TruthSnapshot,
    world: FixtureWorld,
) -> list[str]:
    failures: list[str] = []
    _fail(
        _active_location(after) == "Контора",
        f"contact did not happen in office: {_active_location(after)!r}",
        failures,
    )
    created = _new_characters(before, after)
    _fail(len(created) == 1, f"direct contact created {len(created)} NPCs: {created}", failures)
    if len(created) == 1:
        npc = created[0]
        _fail(npc["status"] == "active", f"new responder is not active: {npc}", failures)
        _fail(
            bool(str(npc.get("description") or "").strip()),
            f"new responder has no description: {npc}",
            failures,
        )
        _fail(
            bool(str(npc.get("appearance") or "").strip()),
            f"new responder has no appearance: {npc}",
            failures,
        )
        _fail(
            npc.get("location") == "Контора",
            f"new responder is not physically in office: {npc}",
            failures,
        )
        _fail("безымян" not in _fold(npc["name"]), f"placeholder identity escaped: {npc}", failures)
    _no_dead_surface(after, failures)
    return failures


def _identity_revealed(
    before: TruthSnapshot,
    after: TruthSnapshot,
    world: FixtureWorld,
) -> list[str]:
    failures: list[str] = []
    created = _new_characters(before, after)
    _fail(len(created) == 1, f"temporary→named reveal split identity: {created}", failures)
    if len(created) == 1:
        npc = created[0]
        name = _fold(npc.get("name"))
        _fail(
            not any(token in name for token in ("безымян", "дежурный", "собеседник")),
            f"identity remained synthetic after explicit name reveal: {npc}",
            failures,
        )
        custom = npc.get("custom_fields") or {}
        _fail(
            custom.get("temporary_name") is not True,
            f"identity is still marked temporary after reveal: {npc}",
            failures,
        )
    _no_dead_surface(after, failures)
    return failures


def _compound_blocked(
    before: TruthSnapshot,
    after: TruthSnapshot,
    world: FixtureWorld,
) -> list[str]:
    failures: list[str] = []
    _fail(
        _active_location(after) == "Коридор",
        f"blocked chain should stop in corridor, got {_active_location(after)!r}",
        failures,
    )
    sequences = after.data["action_sequences"]
    _fail(bool(sequences), "blocked compound input created no ActionSequence", failures)
    if sequences:
        latest = sequences[-1]
        steps = latest.get("steps") or []
        movement = [step for step in steps if step.get("action_type") == "movement"]
        _fail(len(movement) >= 2, f"blocked chain lost a movement step: {latest}", failures)
        if len(movement) >= 2:
            _fail(
                movement[0].get("status") == "completed",
                f"first movement did not complete: {movement}",
                failures,
            )
            _fail(
                movement[1].get("status") in {"blocked", "skipped"},
                f"unavailable second movement was not blocked/skipped: {movement}",
                failures,
            )
        _fail(
            latest.get("completed", 0) < latest.get("planned", 0),
            f"blocked sequence was recorded as fully completed: {latest}",
            failures,
        )
    _no_dead_surface(after, failures)
    return failures


def _undo(client: TestClient, world: FixtureWorld) -> None:
    response = client.post(f"/api/campaigns/{world.campaign_id}/turns/undo")
    if response.status_code != 200:
        raise RuntimeError(f"undo failed: {response.status_code} {response.text}")


def _undo_item_restored(
    before: TruthSnapshot,
    after: TruthSnapshot,
    world: FixtureWorld,
) -> list[str]:
    failures: list[str] = []
    key = after.entity("латунный ключ", entity_type="item")
    _fail(bool(key), "fixture key disappeared after undo", failures)
    if key:
        _fail(key.get("owner") == "Кай", f"undo did not restore key owner: {key}", failures)
        _fail(key.get("location") is None, f"undo left key in world location: {key}", failures)
    return failures


def _undo_npc_removed(
    before: TruthSnapshot,
    after: TruthSnapshot,
    world: FixtureWorld,
) -> list[str]:
    failures: list[str] = []
    _fail(
        _active_location(after) == "Контора",
        f"undo of contact should preserve previous movement turn: {_active_location(after)!r}",
        failures,
    )
    created = _new_characters(before, after)
    _fail(not created, f"undo left turn-introduced NPC durable: {created}", failures)
    return failures


def _fact_created(
    before: TruthSnapshot,
    after: TruthSnapshot,
    world: FixtureWorld,
) -> list[str]:
    failures: list[str] = []
    current = [row for row in after.current_facts() if "свет" in _fold(row.get("subject"))]
    _fail(bool(current), f"explicit changed light state was not persisted: {after.data['facts']}", failures)
    _fail(
        any(
            any(token in _fold(row.get("object")) for token in ("включ", "горит", "заж"))
            for row in current
        ),
        f"current light fact does not represent the observed on-state: {current}",
        failures,
    )
    _no_dead_surface(after, failures)
    return failures


def _undo_fact_removed(
    before: TruthSnapshot,
    after: TruthSnapshot,
    world: FixtureWorld,
) -> list[str]:
    failures: list[str] = []
    current = [row for row in after.current_facts() if "свет" in _fold(row.get("subject"))]
    _fail(not current, f"undo left turn-created light fact current: {current}", failures)
    return failures


def _prepare_locked_door(client: TestClient, world: FixtureWorld) -> None:
    add_fact(
        client,
        world,
        "warehouse_door_locked",
        subject="Дверь склада",
        predicate="состояние",
        object_value="заперта",
        visibility="public",
    )


def _claim_did_not_overwrite_canon(
    before: TruthSnapshot,
    after: TruthSnapshot,
    world: FixtureWorld,
) -> list[str]:
    failures: list[str] = []
    current = [
        row
        for row in after.current_facts()
        if "двер" in _fold(row.get("subject")) and "склад" in _fold(row.get("subject"))
    ]
    _fail(
        any("заперт" in _fold(row.get("object")) for row in current),
        f"player speech displaced established locked-door canon: {current}",
        failures,
    )
    _fail(
        not any("открыт" in _fold(row.get("object")) for row in current),
        f"unsupported player claim became objective fact: {current}",
        failures,
    )
    _no_dead_surface(after, failures)
    return failures


def _event_not_duplicated(
    before: TruthSnapshot,
    after: TruthSnapshot,
    world: FixtureWorld,
) -> list[str]:
    failures: list[str] = []
    before_count = len(before.data["events"])
    created = after.data["events"][before_count:]
    _fail(bool(created), f"completed movement produced no durable event: {after.data['events']}", failures)
    fingerprints = [
        (
            row.get("type"),
            _fold(row.get("description")),
            row.get("location"),
            tuple(row.get("participants") or []),
        )
        for row in created
    ]
    _fail(
        len(fingerprints) == len(set(fingerprints)),
        f"same executed outcome was recorded more than once: {created}",
        failures,
    )
    _no_dead_surface(after, failures)
    return failures


def replacement_cases() -> Sequence[CaseSpec]:
    return (
        CaseSpec(
            id="new_npc_direct_contact",
            title="Unknown responder is contacted only after entering the correct scene",
            transitions=("movement", "character.create", "presence.add", "identity.temporary"),
            turns=(
                "Я выхожу из комнаты в коридор и иду в контору.",
                "В конторе я подхожу к дежурному у стойки и спрашиваю, кто сегодня отвечает за архив.",
            ),
            oracle=_contact_in_office,
            suite="extended",
            min_pass_rate=0.8,
        ),
    )


def additional_cases() -> Sequence[CaseSpec]:
    return (
        CaseSpec(
            id="compound_blocker_stops_tail",
            title="A blocker in step B prevents later compound movement",
            transitions=("compound.block", "movement", "action_sequence.stop"),
            turns=(
                "Я выхожу из комнаты в коридор, а затем пытаюсь пройти прямо из коридора на склад. Прямого прохода из коридора на склад нет.",
            ),
            oracle=_compound_blocked,
        ),
        CaseSpec(
            id="undo_item_drop",
            title="Undo restores deterministic item ownership after a model-driven drop",
            transitions=("undo.item", "item.drop"),
            turns=("Я кладу латунный ключ из руки на пол рядом с собой.",),
            oracle=_undo_item_restored,
            finalize=_undo,
        ),
        CaseSpec(
            id="undo_npc_creation",
            title="Undo removes the NPC introduced by the undone contact turn",
            transitions=("undo.character", "character.create", "presence.add"),
            turns=(
                "Я выхожу из комнаты в коридор и иду в контору.",
                "В конторе я обращаюсь к незнакомому дежурному у стойки: «Добрый вечер».",
            ),
            oracle=_undo_npc_removed,
            finalize=_undo,
            suite="extended",
            min_pass_rate=0.8,
        ),
        CaseSpec(
            id="npc_temporary_to_stable_identity",
            title="Explicit name reveal promotes one temporary NPC instead of creating a duplicate",
            transitions=("identity.temporary", "identity.promote", "character.no_duplicate"),
            turns=(
                "Я выхожу из комнаты в коридор и иду в контору.",
                "В конторе я обращаюсь к незнакомому дежурному у стойки: «Добрый вечер».",
                "Я спрашиваю дежурного: «Как тебя зовут?»",
            ),
            oracle=_identity_revealed,
            suite="extended",
            min_pass_rate=0.7,
        ),
        CaseSpec(
            id="fact_create_from_observable_change",
            title="An explicit observable state change becomes one current world fact",
            transitions=("fact.create",),
            turns=(
                "Я нажимаю обычный исправный выключатель и включаю свет. Лампа загорается, и я это вижу.",
            ),
            oracle=_fact_created,
            suite="extended",
            min_pass_rate=0.8,
        ),
        CaseSpec(
            id="undo_turn_created_fact",
            title="Undo removes a fact that only existed because of the undone turn",
            transitions=("undo.fact", "fact.create"),
            turns=(
                "Я нажимаю обычный исправный выключатель и включаю свет. Лампа загорается, и я это вижу.",
            ),
            oracle=_undo_fact_removed,
            finalize=_undo,
            suite="extended",
            min_pass_rate=0.8,
        ),
        CaseSpec(
            id="player_claim_cannot_overwrite_canon",
            title="Player dialogue does not supersede contradictory objective canon",
            transitions=("canon.contradiction", "fact.preserve"),
            turns=(
                "Я говорю Мартину: «Дверь склада открыта». Я только утверждаю это: дверь не проверяю, к складу не иду и её состояние не меняю.",
            ),
            oracle=_claim_did_not_overwrite_canon,
            prepare=_prepare_locked_door,
            suite="extended",
            min_pass_rate=0.8,
        ),
        CaseSpec(
            id="event_single_write",
            title="One executed movement does not duplicate its durable event",
            transitions=("event.create", "event.no_duplicate", "movement"),
            turns=("Я выхожу из своей комнаты в коридор.",),
            oracle=_event_not_duplicated,
            suite="extended",
            min_pass_rate=0.8,
        ),
    )
