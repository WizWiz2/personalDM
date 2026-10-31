from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.turn import ChatMessage
from app.services.turn_planner import TurnPlanningError
from app.services.linguistic_intent_analyzer import LinguisticIntentAnalysis
from app.services.player_intent_interpreter import (
    PlayerActionIntentDraft,
    PlayerIntentContractDraft,
    PlayerIntentInterpreter,
    _IntentWire,
    normalize_intent_draft,
)


@pytest.mark.asyncio
async def test_every_stated_own_act_is_typed_in_the_one_intent_call():
    text = "Расплачиваюсь, прощаюсь до утра и иду обратно на пристань."
    router = _Router({"summary": text, "actions": [
        {"action_type": "service", "intent": "Расплатиться.", "evidence_quote": "Расплачиваюсь"},
        {"action_type": "other", "intent": "Попрощаться.", "evidence_quote": "прощаюсь до утра"}],
        "stated_act_entries": {"act_0": 0, "act_1": 1, "act_2": 1}})
    await PlayerIntentInterpreter(router).interpret(SimpleNamespace(), [], text)
    assert router.calls == ["PlayerIntentContractDraft"]  # no second model call
    acts = router.schemas[0]["$defs"]["StatedActEntries"]
    assert len(acts["required"]) == 3  # «иду обратно на пристань» must be mapped to an entry


@pytest.mark.asyncio
async def test_a_stated_act_mapped_to_no_entry_fails_closed():
    text = "Расплачиваюсь и иду обратно на пристань."
    router = _Router({"summary": text, "actions": [
        {"action_type": "service", "intent": "Расплатиться.", "evidence_quote": "Расплачиваюсь"}],
        "stated_act_entries": {"act_0": 0, "act_1": 3}})
    with pytest.raises(TurnPlanningError):
        await PlayerIntentInterpreter(router).interpret(SimpleNamespace(), [], text)
    assert router.calls == ["PlayerIntentContractDraft"]


@pytest.mark.asyncio
async def test_locking_workshop_is_local_even_when_extraction_calls_it_movement():
    router = _Router({
        "summary": "Запираю мастерскую и оставляю свет.",
        "actions": [{
            "action_type": "movement", "intent": "Запереть мастерскую.",
            "destination_location": "Мастерская",
        }],
    }, review={
        "action_ownership": [{
            "action_index": 0, "actor_role": "speaker", "contribution_kind": "world_action",
            "spatial_effect": "local", "destination_location": None,
        }],
        "information_request_only": False, "information_recipient": "none",
    })
    result = await _interpreter(router).interpret(SimpleNamespace(), [], "Запираю мастерскую.")
    assert result.actions[0].action_type == "interaction"
    assert result.actions[0].destination_location is None


@pytest.mark.asyncio
async def test_relative_return_can_resolve_without_shared_words():
    room_id = str(uuid4())
    router = _Router({
        "summary": "Возвращаюсь к себе.",
        "actions": [{
            "action_type": "movement", "intent": "Вернуться к себе.",
            "destination_location": "к себе",
        }],
    }, bindings={"action_0": room_id})
    result = await _interpreter(router).interpret(
        SimpleNamespace(), [], "Возвращаюсь к себе.",
        location_references={room_id: "Мастерская Ильи"},
    )
    assert result.actions[0].destination_location == "Мастерская Ильи"


@pytest.mark.asyncio
async def test_ambiguous_direction_cannot_create_a_pronoun_location():
    router = _Router({
        "summary": "Иду наружу.",
        "actions": [{
            "action_type": "movement", "intent": "Иду наружу.", "destination_location": "наружу",
        }],
    }, bindings={"action_0": "unresolved"})
    result = await _interpreter(router).interpret(SimpleNamespace(), [], "Иду наружу.")
    # No location named after a direction, and no trip: the attempt stays a local act.
    assert result.actions[0].action_type == "interaction"
    assert result.actions[0].destination_location is None


class _Router:
    def __init__(
        self,
        payload: dict,
        bindings: dict | None = None,
        review: dict | None = None,
    ):
        self.payload = payload
        self.bindings = bindings
        self.review = review
        self.calls: list[str] = []

    async def generate_json(self, provider, selection, messages, *, response_model, **kwargs):
        """One intent call now carries ownership and place; fixtures fold them into it."""
        del provider, selection, messages, kwargs
        self.calls.append(response_model.__name__)
        self.schemas = [*getattr(self, "schemas", []), response_model.model_json_schema()]
        assert issubclass(response_model, PlayerIntentContractDraft)
        payload = {**self.payload, "actions": [dict(a) for a in self.payload.get("actions", [])]}
        for item in (self.review or {}).get("action_ownership", []):
            action = payload["actions"][item["action_index"]]
            action.update({k: v for k, v in item.items() if k != "action_index" and v is not None})
            if item.get("spatial_effect") in {"local", "none"} and action["action_type"] == "movement":
                action["action_type"] = item.get("action_type") or "interaction"
        if self.review and self.review.get("information_recipient") == "narrator":
            payload["world_state_question"] = True
        if self.review:
            payload["information_request_only"] = self.review["information_request_only"]
        if self.review and self.review["information_request_only"] and self.review.get(
            "information_recipient"
        ) == "character":
            payload["addressed_response_requested"] = True
            payload["addressed_character_name"] = self.review.get("addressed_character_name")
        for key, place in (self.bindings or {}).items():
            payload["actions"][int(key.removeprefix("action_"))]["place"] = place
        if "stated_act_entries" in response_model.model_fields:  # fixtures not about coverage
            payload.setdefault("stated_act_entries", dict.fromkeys(
                response_model.model_fields["stated_act_entries"].annotation.model_fields, 0))
        return payload


class _LinguisticAnalyzer:
    def __init__(self, analysis: LinguisticIntentAnalysis | None = None):
        self.analysis = analysis or LinguisticIntentAnalysis()

    def analyze(
        self,
        player_input: str,
        action_evidence: list[str] | None = None,
    ) -> LinguisticIntentAnalysis:
        del player_input, action_evidence
        return self.analysis

    def stated_acts(self, player_input: str) -> tuple[str, ...]:
        del player_input
        return ()


@pytest.mark.asyncio
@pytest.mark.parametrize("effect", ["inventory", "rest"])
async def test_semantic_effect_review_recovers_durable_operation_from_generic_extraction(effect):
    item_id = str(uuid4())
    fields = ({"item_id": item_id, "inventory_operation": "place"} if effect == "inventory"
              else {"elapsed_time": "восемь часов", "time_after": "morning"})
    text = "Кладу ключ на стол." if effect == "inventory" else "Сплю восемь часов до утра."
    router = _Router(
        {"summary": text, "actions": [{"action_type": "other", "intent": text}]},
        review={
            "action_ownership": [{"action_index": 0, "actor_role": "speaker",
                                  "contribution_kind": "world_action", "action_type": effect,
                                  "spatial_effect": "local" if effect == "inventory" else "none",
                                  "evidence_quote": text, **fields}],
            "information_request_only": False, "information_recipient": "none",
        },
    )
    result = await _interpreter(router).interpret(SimpleNamespace(), [], text)
    assert result.actions[0].action_type == effect
    for name, value in fields.items():
        assert str(getattr(result.actions[0], name)) == value


def _interpreter(
    router: _Router,
    analysis: LinguisticIntentAnalysis | None = None,
) -> PlayerIntentInterpreter:
    return PlayerIntentInterpreter(router, linguistic_analyzer=_LinguisticAnalyzer(analysis))


@pytest.mark.asyncio
async def test_imperative_information_request_is_speech_despite_addressee_syntax():
    player_input = "Назови себя и объясни, откуда знаешь моё имя."
    router = _Router(
        {
            "summary": "Прошу объяснений.",
            "actions": [
                {"action_type": "service", "intent": player_input, "actor_role": "addressee"}
            ],
            "addressed_response_requested": True,
            "addressed_character_name": "Контактное лицо",
        },
        review={
            "action_ownership": [
                {
                    "action_index": 0,
                    "actor_role": "addressee",
                    "contribution_kind": "speech",
                    "evidence_quote": player_input,
                }
            ],
            "information_request_only": True,
            "information_recipient": "character",
            "addressed_character_name": "Контактное лицо",
        },
    )
    result = await _interpreter(
        router,
        LinguisticIntentAnalysis(
            uniform_action_role="addressee",
            action_roles=("addressee",),
            imperative_clauses=(player_input,),
        ),
    ).interpret(SimpleNamespace(), [], player_input)
    assert result.actions == []
    assert result.addressed_response_requested is True
    assert result.addressed_character_name == "Контактное лицо"
    assert result.world_state_question is False


@pytest.mark.asyncio
async def test_mixed_turn_removes_only_speech_candidate_and_preserves_physical_act():
    router = _Router(
        {
            "summary": "Осматриваю раму и спрашиваю дежурного.",
            "actions": [
                {"action_type": "observation", "intent": "Осматриваю раму."},
                {"action_type": "service", "intent": "Объясни, кто закрыл дверь."},
            ],
            "addressed_response_requested": True,
            "addressed_character_name": "Дежурный",
        },
        review={
            "action_ownership": [
                {"action_index": 0, "actor_role": "speaker", "contribution_kind": "world_action"},
                {"action_index": 1, "actor_role": "addressee", "contribution_kind": "speech"},
            ],
            "information_request_only": False,
            "information_recipient": "none",
        },
    )
    result = await _interpreter(router).interpret(
        SimpleNamespace(),
        [],
        "Осматриваю раму. Дежурный, объясни, кто закрыл дверь.",
    )
    assert [(act.action_type, act.intent) for act in result.actions] == [
        ("observation", "Осматриваю раму."),
    ]
    assert result.addressed_response_requested is True


def test_long_dialogue_preserves_contract_without_overflowing_summary():
    input_text = "Объясни, откуда знаешь моё имя. " * 30
    draft = PlayerIntentContractDraft.model_validate(
        {
            "summary": "Прошу рассказать, откуда собеседник знает моё имя.",
            "actions": [],
            "information_request_only": True,
            "addressed_response_requested": True,
        }
    )
    contract = normalize_intent_draft(draft, input_text)
    assert contract.actions == []
    assert contract.addressed_response_requested is True
    assert len(contract.summary) <= 500


@pytest.mark.asyncio
async def test_known_destination_identity_overrides_inflected_model_label():
    location_id = str(uuid4())
    router = _Router(
        {
            "summary": "Возвращаюсь домой.",
            "actions": [
                {
                    "action_type": "movement",
                    "intent": "Вернуться в свою комнату.",
                    "evidence_quote": "возвращаюсь из коридора в свою комнату",
                    "destination_location": "комната Кай",
                }
            ],
        },
        bindings={"action_0": location_id},
    )
    result = await _interpreter(router).interpret(
        SimpleNamespace(),
        [],
        "Возвращаюсь в свою комнату.",
        location_references={location_id: "Комната Кая"},
    )
    assert result.actions[0].destination_location == "Комната Кая"
    assert router.calls == ["PlayerIntentContractDraft"]


@pytest.mark.asyncio
async def test_possessed_room_uses_unique_grammatical_head_not_surrounding_area():
    room_id = str(uuid4())
    surroundings_id = str(uuid4())
    router = _Router(
        {
            "summary": "Возвращаюсь в свою комнату.",
            "actions": [
                {
                    "action_type": "movement",
                    "intent": "Вернуться в свою комнату.",
                    "evidence_quote": "возвращаюсь из коридора в свою комнату",
                    "destination_location": "свою комнату",
                }
            ],
        }, bindings={"action_0": room_id},
    )
    interpreter = PlayerIntentInterpreter(router)

    result = await interpreter.interpret(
        SimpleNamespace(),
        [],
        "Я возвращаюсь из коридора в свою комнату.",
        location_references={
            room_id: "Комната Кая",
            surroundings_id: "Окрестности — Комната Кая",
        },
    )

    assert result.actions[0].destination_location == "Комната Кая"
    assert router.calls == ["PlayerIntentContractDraft"]


@pytest.mark.asyncio
async def test_inflected_destination_reaches_semantic_identity_binding():
    location_id = str(uuid4())
    router = _Router(
        {
            "summary": "Возвращаюсь к стеллажам.",
            "actions": [
                {
                    "action_type": "movement",
                    "intent": "Вернуться к стеллажам.",
                    "evidence_quote": "Возвращаюсь к стеллажам",
                    "destination_location": "стеллажи",
                }
            ],
        },
        bindings={"action_0": location_id},
    )
    result = await PlayerIntentInterpreter(router).interpret(
        SimpleNamespace(),
        [],
        "Возвращаюсь к стеллажам тем же проходом.",
        location_references={location_id: "стеллажам"},
    )
    assert result.actions[0].destination_location == "стеллажам"
    assert router.calls == ["PlayerIntentContractDraft"]


@pytest.mark.asyncio
async def test_unknown_location_identity_cannot_authorize_a_destination():
    router = _Router(
        {
            "summary": "Иду в комнату.",
            "actions": [
                {
                    "action_type": "movement",
                    "intent": "Иду в комнату.",
                    "destination_location": "Моя комната",
                }
            ],
        },
        bindings={"action_0": str(uuid4())},
    )
    result = await _interpreter(router).interpret(
        SimpleNamespace(), [], "Иду в комнату.", location_references={str(uuid4()): "Комната"},
    )
    # The deterministic binder rejects an uncatalogued ID: no topology, no trip.
    assert result.actions[0].action_type == "interaction"
    assert result.actions[0].destination_location is None


@pytest.mark.asyncio
async def test_new_destination_binding_preserves_selected_endpoint_and_action():
    router = _Router(
        {
            "summary": "Иду в прачечную.",
            "actions": [
                {
                    "action_type": "movement",
                    "intent": "Иду в прачечную.",
                    "destination_location": "Прачечная соседнего дома",
                }
            ],
        },
        bindings={"action_0": "new"},
    )
    result = await _interpreter(router).interpret(
        SimpleNamespace(),
        [],
        "Иду в прачечную соседнего дома.",
        location_references={str(uuid4()): "Контора"},
    )
    assert len(result.actions) == 1
    assert result.actions[0].destination_location == "Прачечная соседнего дома"
    assert result.actions[0].intent == "Иду в прачечную."
    assert router.calls == ["PlayerIntentContractDraft"]


@pytest.mark.asyncio
async def test_healthy_intent_path_binds_identity_by_id() -> None:
    corridor_id = str(uuid4())
    router = _Router(
        {
            "summary": "Кай выходит в коридор.",
            "actions": [
                {
                    "action_type": "movement",
                    "intent": "Выйти в коридор.",
                    "destination_location": "Коридор",
                }
            ],
        },
        bindings={"action_0": corridor_id},
    )
    interpreter = _interpreter(router)

    result = await interpreter.interpret(
        SimpleNamespace(),
        [ChatMessage(role="system", content="AUTHORITATIVE STATE")],
        "Я выхожу в коридор.",
        location_references={corridor_id: "Коридор"},
    )

    assert result.actions[0].destination_location == "Коридор"
    assert str(result.actions[0].destination_location_id) == corridor_id
    assert router.calls == ["PlayerIntentContractDraft"]
    assert interpreter.audit[-1]["phase"] == "single_pass"
    assert interpreter.audit[-1]["normalization"] == "deterministic"


def test_typed_inventory_operation_can_recover_mislabelled_model_action() -> None:
    item_id = uuid4()
    draft = PlayerIntentContractDraft.model_validate(
        {
            "summary": "Кай кладёт ключ на пол.",
            "actions": [
                {
                    "action_type": "movement",
                    "intent": "Кладу латунный ключ на пол.",
                    "item_id": str(item_id),
                    "inventory_operation": "drop",
                }
            ],
        }
    )

    result = normalize_intent_draft(draft, "Кладу латунный ключ на пол.")

    action = result.actions[0]
    assert action.action_type == "inventory"
    assert action.item_id == item_id
    assert action.inventory_operation == "drop"
    assert action.destination_location is None


def test_irrelevant_inventory_fields_are_removed_from_non_inventory_action() -> None:
    draft = PlayerIntentContractDraft.model_validate(
        {
            "summary": "Кай нажимает кнопку.",
            "actions": [
                {
                    "action_type": "interaction",
                    "intent": "Нажать кнопку.",
                    "item_id": str(uuid4()),
                }
            ],
        }
    )

    action = normalize_intent_draft(draft, "Нажимаю кнопку.").actions[0]

    assert action.action_type == "interaction"
    assert action.item_id is None
    assert action.inventory_operation is None
    assert action.inventory_target_id is None


def test_typed_give_operation_is_preserved() -> None:
    item_id = uuid4()
    target_id = uuid4()
    draft = PlayerIntentContractDraft.model_validate(
        {
            "summary": "Кай отдаёт ключ Мартину.",
            "actions": [
                {
                    "action_type": "inventory",
                    "intent": "Отдаю латунный ключ Мартину.",
                    "item_id": str(item_id),
                    "inventory_operation": "give",
                    "inventory_target_id": str(target_id),
                }
            ],
        }
    )

    action = normalize_intent_draft(draft, "Отдаю латунный ключ Мартину.").actions[0]

    assert action.inventory_operation == "give"
    assert action.item_id == item_id
    assert action.inventory_target_id == target_id


def test_movement_without_destination_becomes_a_local_act_not_a_failure() -> None:
    draft = PlayerIntentContractDraft.model_validate(
        {
            "summary": "Кай куда-то идёт.",
            "actions": [{"action_type": "movement", "intent": "Иду дальше."}],
        }
    )

    action = normalize_intent_draft(draft, "Иду дальше.").actions[0]
    assert action.action_type == "interaction"
    assert action.destination_location is None


@pytest.mark.asyncio
async def test_semantic_review_assigns_command_to_addressee() -> None:
    player_input = "Я так понимаю, ответ — да. Тогда раздевайтесь."
    router = _Router(
        {
            "summary": "Кай понимает, что ответ — да, и начинает раздеваться.",
            "actions": [
                {
                    "action_type": "other",
                    "intent": "начинаю раздеваться",
                }
            ],
        },
        review={
            "action_ownership": [
                {
                    "action_index": 0,
                    "actor_role": "addressee",
                    "evidence_quote": "Тогда раздевайтесь",
                }
            ],
            "information_request_only": False,
            "information_recipient": "none",
            "addressed_character_name": None,
        },
    )

    result = await _interpreter(router).interpret(
        SimpleNamespace(),
        [],
        player_input,
    )

    assert result.actions[0].action_type == "service"
    assert result.addressed_response_requested is True
    assert "раздевайтесь" in result.summary


@pytest.mark.asyncio
async def test_semantic_review_prevents_addressee_command_from_mutating_player_inventory() -> None:
    player_input = "Хорошо, это тоже снимайте."
    router = _Router(
        {
            "summary": "Кай просит снять заметную деталь.",
            "actions": [
                {
                    "action_type": "inventory",
                    "intent": "снять заметную деталь",
                    "item_id": str(uuid4()),
                    "inventory_operation": "take",
                }
            ],
        },
        review={
            "action_ownership": [
                {
                    "action_index": 0,
                    "actor_role": "addressee",
                    "evidence_quote": "это тоже снимайте",
                }
            ],
            "information_request_only": False,
            "information_recipient": "none",
            "addressed_character_name": None,
        },
    )

    result = await _interpreter(router).interpret(
        SimpleNamespace(),
        [],
        player_input,
    )

    assert result.actions[0].action_type == "service"
    assert result.actions[0].item_id is None
    assert result.addressed_response_requested is True


def test_player_own_undress_still_survives() -> None:
    draft = PlayerIntentContractDraft.model_validate(
        {
            "summary": "Кай раздевается.",
            "actions": [
                {
                    "action_type": "other",
                    "intent": "Я раздеваюсь.",
                }
            ],
        }
    )

    result = normalize_intent_draft(draft, "Я раздеваюсь.")

    assert len(result.actions) == 1


@pytest.mark.asyncio
async def test_semantic_review_types_description_request_as_information_only() -> None:
    player_input = "Ты описываешь только лицо. Я хочу подробности касательно их тел."
    router = _Router(
        {
            "summary": "Кай рассматривает присутствующих.",
            "actions": [
                {
                    "action_type": "observation",
                    "actor_role": "speaker",
                    "intent": "Рассмотреть тела присутствующих.",
                }
            ],
        },
        review={
            "action_ownership": [
                {
                    "action_index": 0,
                    "actor_role": "speaker",
                    "evidence_quote": "Я хочу подробности касательно их тел",
                }
            ],
            "information_request_only": True,
            "information_recipient": "narrator",
            "addressed_character_name": None,
        },
    )

    result = await _interpreter(router).interpret(
        SimpleNamespace(),
        [],
        player_input,
    )

    assert result.actions == []
    assert result.world_state_question is True


def test_addressee_act_is_service_and_keeps_the_speaker() -> None:
    player_input = "Анна, подай кувшин со стола. Я сам его не беру."
    draft = PlayerIntentContractDraft(
        summary="Анна просит кувшин со стола, но сама его не берет.",
        actions=[
            PlayerActionIntentDraft(
                action_type="inventory",
                actor_role="addressee",
                intent="подай кувшин со стола",
                item_id=str(uuid4()),
                inventory_operation="take",
            )
        ],
        addressed_character_name="Анна",
    )
    contract = normalize_intent_draft(draft, player_input)
    assert len(contract.actions) == 1
    action = contract.actions[0]
    assert action.action_type == "service"
    assert action.item_id is None
    assert action.inventory_operation is None
    assert action.intent == "подай кувшин со стола"
    assert contract.summary == player_input
    assert contract.addressed_response_requested is True
    assert contract.addressed_character_name == "Анна"


def test_quote_and_kind_precede_actor_on_the_model_schema() -> None:
    schema = _IntentWire.model_json_schema()
    action_refs = [item["$ref"] for item in schema["properties"]["actions"]["items"]["anyOf"]]
    for ref in action_refs:
        name = ref.rsplit("/", 1)[-1]
        definition = schema["$defs"][name]
        assert list(definition["properties"])[:2] == ["evidence_quote", "contribution_kind"]
        assert "actor_id" in definition["required"]


@pytest.mark.asyncio
async def test_empty_action_turn_skips_ownership_model_and_empty_literal() -> None:
    router = _Router({"summary": "Я киваю.", "actions": []})

    result = await _interpreter(router).interpret(SimpleNamespace(), [], "Я киваю.")

    assert result.actions == []
    assert router.calls == ["PlayerIntentContractDraft"]


@pytest.mark.asyncio
async def test_inconsistent_recipient_and_paraphrased_quote_do_not_abort_turn() -> None:
    router = _Router(
        {
            "summary": "Я открываю дверь.",
            "actions": [{"action_type": "interaction", "intent": "Открыть дверь."}],
        },
        review={
            "action_ownership": [
                {
                    "action_index": 0,
                    "actor_role": "speaker",
                    "evidence_quote": "Открываю дверь",
                }
            ],
            "information_request_only": False,
            "information_recipient": "character",
            "addressed_character_name": None,
        },
    )

    result = await _interpreter(router).interpret(SimpleNamespace(), [], "Я открываю дверь.")

    assert result.actions[0].action_type == "interaction"
    assert result.addressed_response_requested is False


@pytest.mark.asyncio
async def test_world_state_question_cannot_become_a_new_action() -> None:
    player_input = "В чем сейчас Мария? Она разделась до гола?"
    router = _Router(
        {
            "summary": "Мария раздевается до гола.",
            "actions": [
                {
                    "action_type": "other",
                    "actor_role": "speaker",
                    "intent": "Мария раздевается до гола.",
                }
            ],
        },
        review={
            "action_ownership": [
                {
                    "action_index": 0,
                    "actor_role": "speaker",
                    "evidence_quote": "Она разделась до гола?",
                }
            ],
            "information_request_only": True,
            "information_recipient": "narrator",
            "addressed_character_name": None,
        },
    )

    result = await _interpreter(
        router,
        LinguisticIntentAnalysis(
            information_request_only=True,
            information_recipient="narrator",
        ),
    ).interpret(
        SimpleNamespace(),
        [],
        player_input,
    )

    assert result.actions == []
    assert result.world_state_question is True
    assert result.addressed_response_requested is False
    assert result.summary == "В чем сейчас Мария? Она разделась до гола?"


@pytest.mark.asyncio
@pytest.mark.parametrize("number", [7, 21, 137])
async def test_numbered_destination_is_bound_by_semantic_id(number):
    location_id = str(uuid4())
    selected = f"причал номер {number}"
    persisted = f"Причал №{number}"
    router = _Router(
        {"summary": "Вернуться на известный причал.", "actions": [{
            "action_type": "movement", "intent": "Вернуться на причал.", "evidence_quote": "Возвращаюсь",
            "destination_location": selected,
        }]},
        bindings={"action_0": location_id},
    )
    result = await PlayerIntentInterpreter(router).interpret(
        SimpleNamespace(), [], f"Возвращаюсь на {selected}.",
        location_references={location_id: persisted},
    )
    assert result.actions[0].destination_location == persisted
    assert router.calls == ["PlayerIntentContractDraft"]


@pytest.mark.asyncio
async def test_new_destination_binding_carries_no_catalogued_identity():
    office_id = str(uuid4())
    router = _Router(
        {"summary": "Выйти из здания и дойти до прачечной.", "actions": [{
            "action_type": "movement", "intent": "Выйти из здания.",
            "destination_location": "наружу",
        }]},
        bindings={"action_0": "new"},
        review={
            "action_ownership": [{
                "action_index": 0, "actor_role": "speaker", "contribution_kind": "world_action",
                "action_type": "movement", "spatial_effect": "travel",
                "destination_location": "круглосуточная прачечная соседнего дома",
            }],
            "information_request_only": False, "information_recipient": "none",
        },
    )
    result = await _interpreter(router).interpret(
        SimpleNamespace(), [], "Выхожу из здания и иду в прачечную соседнего дома.",
        location_references={office_id: "Контора"},
    )
    assert result.actions[0].destination_location == "круглосуточная прачечная соседнего дома"
    assert result.actions[0].destination_location_id is None


@pytest.mark.asyncio
async def test_a_trailing_question_cannot_erase_typed_physical_acts():
    """diag T1: one sentence ending in «?» parsed as all-interrogative and the syntax override set
    information_request_only, dropping the climb and the bag placement. The narrator rendered them,
    the validator rejected, and the published text said the bag stayed with the hero."""
    text = "Поднимаюсь в комнату, кладу сумку трав у кровати и спрашиваю Борма через перила: «Во сколько завтрак?»"
    router = _Router({"summary": text, "actions": [
        {"action_type": "interaction", "intent": "Положить сумку у кровати.",
         "evidence_quote": "кладу сумку трав у кровати", "contribution_kind": "world_action"},
        {"action_type": "other", "intent": "Спросить про завтрак.",
         "evidence_quote": "спрашиваю Борма через перила: «Во сколько завтрак?»", "contribution_kind": "speech"}],
        "information_request_only": True})
    interpreter = PlayerIntentInterpreter(router)
    interpreter._linguistic_analyzer.analyze = lambda *a, **k: LinguisticIntentAnalysis(
        uniform_action_role="speaker", action_roles=("speaker", "speaker"), imperative_clauses=(),
        information_request_only=True, information_recipient="narrator")
    result = await interpreter.interpret(SimpleNamespace(), [], text)
    assert [act.intent for act in result.actions] == ["Положить сумку у кровати."]
