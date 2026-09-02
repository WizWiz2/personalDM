from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

from fastapi.testclient import TestClient

from live_model_contracts.snapshot import TruthSnapshot
from live_model_contracts.world import (
    FixtureWorld,
    add_belief,
    add_fact,
    transfer_item,
)

Prepare = Callable[[TestClient, FixtureWorld], None]
Finalize = Callable[[TestClient, FixtureWorld], None]
Oracle = Callable[[TruthSnapshot, TruthSnapshot, FixtureWorld], list[str]]


@dataclass(frozen=True)
class CaseSpec:
    id: str
    title: str
    transitions: tuple[str, ...]
    turns: tuple[str, ...]
    oracle: Oracle
    prepare: Prepare | None = None
    finalize: Finalize | None = None
    suite: str = "core"
    min_pass_rate: float = 1.0


def _fold(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def _fail(condition: bool, message: str, failures: list[str]) -> None:
    if not condition:
        failures.append(message)


def _active_location(snapshot: TruthSnapshot) -> str | None:
    scene = snapshot.active_scene()
    return str(scene.get("location")) if scene and scene.get("location") else None


def _character(snapshot: TruthSnapshot, name: str) -> dict | None:
    return snapshot.entity(name, entity_type="character")


def _item(snapshot: TruthSnapshot, needle: str) -> dict | None:
    key = needle.casefold()
    return next(
        (
            row
            for row in snapshot.entity_rows(entity_type="item")
            if key in row["name"].casefold()
        ),
        None,
    )


def _locations(snapshot: TruthSnapshot) -> list[dict]:
    return snapshot.entity_rows(entity_type="location")


def _no_dead_surface(snapshot: TruthSnapshot, failures: list[str]) -> None:
    surface = _fold(snapshot.assistant_surface)
    forbidden = (
        "пока ничего заметно не меняется",
        "ничего не происходит",
        "пока ничего не меняется",
    )
    _fail(bool(surface), "assistant produced no active published surface", failures)
    _fail(not any(value in surface for value in forbidden), f"dead-turn surface escaped: {surface!r}", failures)


def _movement_known(before: TruthSnapshot, after: TruthSnapshot, world: FixtureWorld) -> list[str]:
    failures: list[str] = []
    _fail(_active_location(after) == "Коридор", f"active location is {_active_location(after)!r}, expected 'Коридор'", failures)
    kai = _character(after, "Кай")
    _fail(bool(kai and kai.get("location") == "Коридор"), f"Kai location after movement: {kai}", failures)
    martin = _character(after, "Мартин Вэнс")
    _fail(bool(martin and martin.get("location") == "Комната Кая"), f"Martin followed/leaked from room: {martin}", failures)
    active = after.active_scene() or {}
    _fail("Мартин Вэнс" not in active.get("participants", []), f"Martin is present in destination scene: {active}", failures)
    _fail(len(_locations(after)) == len(_locations(before)), "known movement created a duplicate/new Location", failures)
    _no_dead_surface(after, failures)
    return failures


def _return_known(before: TruthSnapshot, after: TruthSnapshot, world: FixtureWorld) -> list[str]:
    failures: list[str] = []
    _fail(_active_location(after) == "Комната Кая", f"return did not reuse known room: {_active_location(after)!r}", failures)
    _fail(len(_locations(after)) == len(_locations(before)), "round-trip created duplicate locations", failures)
    room_rows = [row for row in _locations(after) if row["name"] == "Комната Кая"]
    _fail(len(room_rows) == 1, f"room identity split: {room_rows}", failures)
    _no_dead_surface(after, failures)
    return failures


def _new_location(before: TruthSnapshot, after: TruthSnapshot, world: FixtureWorld) -> list[str]:
    failures: list[str] = []
    before_ids = {row["id"] for row in _locations(before)}
    created = [row for row in _locations(after) if row["id"] not in before_ids]
    _fail(len(created) == 1, f"expected exactly one discovered Location, got {created}", failures)
    if len(created) == 1:
        description = " ".join(str(created[0].get("description") or "").split())
        _fail(len(description) >= 80, f"new Location has no durable profile: {created[0]}", failures)
        _fail(_active_location(after) == created[0]["name"], f"new Location exists but is not active: {created[0]}", failures)
    _no_dead_surface(after, failures)
    return failures


def _compound(before: TruthSnapshot, after: TruthSnapshot, world: FixtureWorld) -> list[str]:
    failures: list[str] = []
    _fail(_active_location(after) == "Контора", f"compound movement ended at {_active_location(after)!r}", failures)
    sequences = after.data["action_sequences"]
    _fail(bool(sequences), "compound player input created no ActionSequence", failures)
    if sequences:
        latest = sequences[-1]
        movement_steps = [step for step in latest["steps"] if step["action_type"] == "movement"]
        _fail(len(movement_steps) >= 2, f"compound sequence lost a movement step: {latest}", failures)
        _fail(all(step["status"] == "completed" for step in movement_steps[:2]), f"movement steps were not both completed: {movement_steps}", failures)
    _no_dead_surface(after, failures)
    return failures


def _dead_stays_dead(before: TruthSnapshot, after: TruthSnapshot, world: FixtureWorld) -> list[str]:
    failures: list[str] = []
    lydia = _character(after, "Лидия")
    _fail(bool(lydia), "dead Lydia disappeared from durable identity registry", failures)
    if lydia:
        _fail(lydia["status"] == "dead", f"dead Lydia changed status: {lydia}", failures)
        _fail(lydia.get("location") is None, f"dead Lydia acquired physical location: {lydia}", failures)
    active = after.active_scene() or {}
    _fail("Лидия" not in active.get("participants", []), f"dead Lydia materialized into scene: {active}", failures)
    _fail(len(after.entity_rows(entity_type="character", name="Лидия")) == 1, "Lydia identity was duplicated", failures)
    _no_dead_surface(after, failures)
    return failures


def _new_contact(before: TruthSnapshot, after: TruthSnapshot, world: FixtureWorld) -> list[str]:
    failures: list[str] = []
    before_ids = {row["id"] for row in before.entity_rows(entity_type="character")}
    created = [row for row in after.entity_rows(entity_type="character") if row["id"] not in before_ids]
    _fail(len(created) == 1, f"direct contact should type exactly one responder, got {created}", failures)
    if len(created) == 1:
        npc = created[0]
        _fail(npc["status"] == "active", f"new responder is not active: {npc}", failures)
        _fail(bool(str(npc.get("description") or "").strip()), f"new responder has no description: {npc}", failures)
        _fail(bool(str(npc.get("appearance") or "").strip()), f"new responder has no appearance: {npc}", failures)
        _fail(npc.get("location") == _active_location(after), f"new responder is not located in active scene: {npc}", failures)
        _fail("безымян" not in _fold(npc["name"]), f"synthetic placeholder became identity: {npc}", failures)
    _no_dead_surface(after, failures)
    return failures


def _item_owner(expected_owner: str | None, expected_location: str | None) -> Oracle:
    def check(before: TruthSnapshot, after: TruthSnapshot, world: FixtureWorld) -> list[str]:
        failures: list[str] = []
        item = _item(after, "латунный ключ")
        _fail(bool(item), "fixture key disappeared", failures)
        if item:
            _fail(item.get("owner") == expected_owner, f"item owner={item.get('owner')!r}, expected {expected_owner!r}", failures)
            _fail(item.get("location") == expected_location, f"item location={item.get('location')!r}, expected {expected_location!r}", failures)
        _no_dead_surface(after, failures)
        return failures

    return check


def _prepare_key_on_floor(client: TestClient, world: FixtureWorld) -> None:
    transfer_item(client, world, "латунный ключ", location="Комната Кая")


def _time_advance(before: TruthSnapshot, after: TruthSnapshot, world: FixtureWorld) -> list[str]:
    failures: list[str] = []
    old = before.data.get("scene_state") or {}
    new = after.data.get("scene_state") or {}
    _fail(bool(new), "active scene has no runtime time state after rest", failures)
    if new:
        _fail(int(new.get("world_time_order") or 0) > int(old.get("world_time_order") or 0), f"time did not advance: before={old}, after={new}", failures)
    _no_dead_surface(after, failures)
    return failures


def _time_stable(before: TruthSnapshot, after: TruthSnapshot, world: FixtureWorld) -> list[str]:
    failures: list[str] = []
    old = before.data.get("scene_state") or {}
    new = after.data.get("scene_state") or {}
    _fail(new.get("world_time_order") == old.get("world_time_order"), f"observation advanced world time: before={old}, after={new}", failures)
    _no_dead_surface(after, failures)
    return failures


def _prepare_claim(client: TestClient, world: FixtureWorld) -> None:
    add_belief(
        client,
        world,
        "Мартин Вэнс",
        "По моему мнению склад принадлежит компании «Север», но я не показывал документы, подтверждающие это.",
    )


def _claim_is_knowledge(before: TruthSnapshot, after: TruthSnapshot, world: FixtureWorld) -> list[str]:
    failures: list[str] = []
    hero_beliefs = after.current_beliefs("Кай")
    sourced = [row for row in hero_beliefs if row.get("source_character") == "Мартин Вэнс"]
    _fail(bool(sourced), f"Martin's spoken claim was not attributed to Martin in Kai knowledge: {hero_beliefs}", failures)
    ownership_facts = [
        row
        for row in after.current_facts()
        if "склад" in _fold(row.get("subject")) and any(token in _fold(row) for token in ("принадлеж", "север", "владел"))
    ]
    _fail(not ownership_facts, f"NPC claim was promoted to objective world fact: {ownership_facts}", failures)
    _no_dead_surface(after, failures)
    return failures


def _prepare_light_fact(client: TestClient, world: FixtureWorld) -> None:
    add_fact(
        client,
        world,
        "light_off",
        subject="Свет в комнате Кая",
        predicate="состояние",
        object_value="выключен",
    )


def _fact_supersedes(before: TruthSnapshot, after: TruthSnapshot, world: FixtureWorld) -> list[str]:
    failures: list[str] = []
    current = [row for row in after.current_facts() if "свет" in _fold(row.get("subject"))]
    _fail(bool(current), f"light state vanished from current facts: {after.data['facts']}", failures)
    _fail(not any("выключ" in _fold(row.get("object")) for row in current), f"old light-off fact remained current: {current}", failures)
    _fail(any(any(token in _fold(row.get("object")) for token in ("включ", "горит", "заж")) for row in current), f"new light-on state was not persisted: {current}", failures)
    _no_dead_surface(after, failures)
    return failures


def _concrete_negative(before: TruthSnapshot, after: TruthSnapshot, world: FixtureWorld) -> list[str]:
    failures: list[str] = []
    _no_dead_surface(after, failures)
    surface = _fold(after.assistant_surface)
    _fail(len(surface) >= 40, f"investigation returned an implausibly empty surface: {surface!r}", failures)
    latest_generation = after.data["generations"][-1] if after.data["generations"] else None
    _fail(bool(latest_generation and latest_generation["status"] == "completed"), f"generation did not complete: {latest_generation}", failures)
    return failures


def _undo_movement(client: TestClient, world: FixtureWorld) -> None:
    response = client.post(f"/api/campaigns/{world.campaign_id}/turns/undo")
    if response.status_code != 200:
        raise RuntimeError(f"undo failed: {response.status_code} {response.text}")


def _undo_restores(before: TruthSnapshot, after: TruthSnapshot, world: FixtureWorld) -> list[str]:
    failures: list[str] = []
    _fail(_active_location(after) == "Комната Кая", f"undo did not restore player scene: {_active_location(after)!r}", failures)
    kai = _character(after, "Кай")
    _fail(bool(kai and kai.get("location") == "Комната Кая"), f"undo did not restore Kai location: {kai}", failures)
    active_assistant = [row for row in after.data["turns"] if row["role"] == "assistant" and row["status"] == "active"]
    _fail(not active_assistant, f"undone assistant turn remains active: {active_assistant}", failures)
    return failures


def _prepare_relationship(client: TestClient, world: FixtureWorld) -> None:
    response = client.post(
        f"/api/campaigns/{world.campaign_id}/relationships",
        json={
            "subject_id": world.characters["Мартин Вэнс"],
            "object_id": world.characters["Кай"],
            "relation_type": "debt",
            "description": "Кай должен Мартину вернуть латунный ключ; после возврата долг закрыт.",
            "reason": "Явное условие договора между ними.",
            "intensity": -0.4,
            "visibility": "public",
        },
    )
    if response.status_code != 201:
        raise RuntimeError(f"relationship fixture failed: {response.status_code} {response.text}")


def _relationship_resolved(before: TruthSnapshot, after: TruthSnapshot, world: FixtureWorld) -> list[str]:
    failures: list[str] = []
    current = [
        row
        for row in after.current_relationships()
        if row.get("subject") == "Мартин Вэнс" and row.get("object") == "Кай"
    ]
    unresolved = [row for row in current if row.get("type") == "debt" and any(token in _fold(row.get("description")) for token in ("долж", "вернуть"))]
    _fail(not unresolved, f"explicitly satisfied debt remained current: {unresolved}", failures)
    _no_dead_surface(after, failures)
    return failures


def _prepare_theses(client: TestClient, world: FixtureWorld) -> None:
    for text in (
        "Кай должен вернуть Мартину латунный ключ.",
        "Нужно выяснить, кто оставил отметку на двери склада.",
        "Нужно проверить происхождение старой квитанции.",
        "Нужно понять, почему в журнале есть семиминутный разрыв.",
    ):
        response = client.post(
            f"/api/scenes/{world.scene_id}/theses",
            json={"thesis_type": "unresolved_beat", "text": text, "priority": 8, "visibility": "public"},
        )
        if response.status_code != 201:
            raise RuntimeError(f"thesis fixture failed: {response.status_code} {response.text}")


def _thesis_resolves_one(before: TruthSnapshot, after: TruthSnapshot, world: FixtureWorld) -> list[str]:
    failures: list[str] = []
    before_active = before.active_theses()
    after_active = after.active_theses()
    _fail(len(before_active) >= 4, f"fixture did not start with four live theses: {before_active}", failures)
    key_open = [row for row in after_active if "вернуть" in _fold(row.get("text")) and "ключ" in _fold(row.get("text"))]
    _fail(not key_open, f"completed key-return thesis remained active: {key_open}", failures)
    for anchor in ("отметк", "квитанц", "семиминут"):
        _fail(any(anchor in _fold(row.get("text")) for row in after_active), f"independent thesis {anchor!r} was lost: {after_active}", failures)
    return failures


def all_cases() -> Sequence[CaseSpec]:
    return (
        CaseSpec(
            id="movement_known_location",
            title="Known movement changes scene/presence without duplicating locations",
            transitions=("scene.complete", "scene.create", "movement", "presence.remove"),
            turns=("Я выхожу из своей комнаты в коридор.",),
            oracle=_movement_known,
        ),
        CaseSpec(
            id="movement_round_trip_identity",
            title="A→B→A reuses durable Location identity",
            transitions=("movement", "location.revisit", "scene.return"),
            turns=("Я выхожу из своей комнаты в коридор.", "Я возвращаюсь из коридора в свою комнату."),
            oracle=_return_known,
        ),
        CaseSpec(
            id="movement_new_location_profile",
            title="Explicit discovery creates one described Location",
            transitions=("location.create", "scene.create", "movement"),
            turns=(
                "Я выхожу из здания и иду в круглосуточную прачечную на первом этаже соседнего дома. Это обычный открытый маршрут.",
            ),
            oracle=_new_location,
        ),
        CaseSpec(
            id="compound_two_movements",
            title="Ordered movement keeps both structural steps",
            transitions=("action_sequence.create", "movement", "movement"),
            turns=("Я выхожу из комнаты в коридор, затем сразу иду из коридора в контору.",),
            oracle=_compound,
        ),
        CaseSpec(
            id="dead_character_mention",
            title="Mentioning a dead NPC never resurrects physical presence",
            transitions=("entity.dead.invariant", "presence.no_add"),
            turns=("Я вспоминаю Лидию, которая погибла два года назад, и рассказываю Мартину о ней.",),
            oracle=_dead_stays_dead,
        ),
        CaseSpec(
            id="new_npc_direct_contact",
            title="Positive contact with an unknown role materializes one profiled NPC",
            transitions=("character.create", "presence.add", "identity.temporary"),
            turns=("Я подхожу к дежурному у стойки в конторе и спрашиваю, кто сегодня отвечает за архив.",),
            oracle=_new_contact,
            # First move isolates the contact in the known office; the second turn performs contact.
            prepare=None,
            suite="extended",
            min_pass_rate=0.8,
        ),
        CaseSpec(
            id="item_drop",
            title="Owned item can be dropped into current Location",
            transitions=("item.drop",),
            turns=("Я кладу латунный ключ из своей руки на пол рядом с собой.",),
            oracle=_item_owner(None, "Комната Кая"),
        ),
        CaseSpec(
            id="item_take",
            title="Present item can be taken by player",
            transitions=("item.take",),
            turns=("Я поднимаю лежащий рядом латунный ключ и беру его себе.",),
            oracle=_item_owner("Кай", None),
            prepare=_prepare_key_on_floor,
        ),
        CaseSpec(
            id="item_give",
            title="Owned item can be given to present NPC",
            transitions=("item.give",),
            turns=("Я передаю латунный ключ Мартину Вэнсу.",),
            oracle=_item_owner("Мартин Вэнс", None),
        ),
        CaseSpec(
            id="item_place",
            title="Place operation removes ownership and locates item in scene",
            transitions=("item.place",),
            turns=("Я аккуратно кладу латунный ключ на рабочий стол и убираю руку.",),
            oracle=_item_owner(None, "Комната Кая"),
        ),
        CaseSpec(
            id="time_explicit_advance",
            title="Explicit long rest advances structured world time",
            transitions=("time.advance", "scene.time_transition"),
            turns=("Я ложусь спать на восемь часов и просыпаюсь утром.",),
            oracle=_time_advance,
        ),
        CaseSpec(
            id="time_no_accidental_advance",
            title="Observation does not advance structured time",
            transitions=("time.no_change",),
            turns=("Я стою на месте и внимательно осматриваю рабочий стол.",),
            oracle=_time_stable,
        ),
        CaseSpec(
            id="npc_claim_epistemics",
            title="NPC claim becomes sourced knowledge, not objective canon",
            transitions=("knowledge.create", "fact.no_create", "speaker.attribution"),
            turns=("Я спрашиваю Мартина: «Кому, по твоему мнению, принадлежит склад?»",),
            oracle=_claim_is_knowledge,
            prepare=_prepare_claim,
            suite="extended",
            min_pass_rate=0.8,
        ),
        CaseSpec(
            id="fact_state_supersede",
            title="Changed world state supersedes its previous current fact",
            transitions=("fact.supersede",),
            turns=("Я нажимаю обычный исправный выключатель и включаю свет в комнате.",),
            oracle=_fact_supersedes,
            prepare=_prepare_light_fact,
            suite="extended",
            min_pass_rate=0.8,
        ),
        CaseSpec(
            id="negative_result_is_concrete",
            title="Investigation cannot collapse into generic no-change fiction",
            transitions=("turn.publish.concrete",),
            turns=("Я поднимаю системные журналы и ищу следы взлома и зацепки, к которым они могут привести.",),
            oracle=_concrete_negative,
        ),
        CaseSpec(
            id="undo_movement",
            title="Undo restores scene and player location after model-driven movement",
            transitions=("undo.movement", "undo.scene"),
            turns=("Я выхожу из своей комнаты в коридор.",),
            oracle=_undo_restores,
            finalize=_undo_movement,
        ),
        CaseSpec(
            id="relationship_explicit_resolution",
            title="Satisfied explicit debt does not remain current",
            transitions=("relationship.supersede",),
            turns=("Я возвращаю Мартину Вэнсу латунный ключ, полностью выполняя условие нашего долга.",),
            oracle=_relationship_resolved,
            prepare=_prepare_relationship,
            suite="extended",
            min_pass_rate=0.7,
        ),
        CaseSpec(
            id="thesis_resolve_exactly_one",
            title="Resolved thread closes while independent same-type threads survive",
            transitions=("thesis.resolve", "thesis.preserve"),
            turns=(
                "Я возвращаю Мартину латунный ключ, закрывая этот вопрос.",
                "Я остаюсь на месте и спрашиваю Мартина, всё ли с возвратом ключа закончено.",
                "Я киваю и не предпринимаю никаких новых действий.",
            ),
            oracle=_thesis_resolves_one,
            prepare=_prepare_theses,
            suite="extended",
            min_pass_rate=0.7,
        ),
    )
