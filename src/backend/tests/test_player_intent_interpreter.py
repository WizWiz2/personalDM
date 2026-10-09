from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.turn import ChatMessage
from app.services.linguistic_intent_analyzer import LinguisticIntentAnalysis
from app.services.player_intent_interpreter import (
    PlayerActionIntentDraft,
    PlayerIntentContractDraft,
    PlayerIntentInterpreter,
    _IntentWire,
    _intent_semantic_review_wire,
    normalize_intent_draft,
)
from app.services.turn_planner import TurnPlanningError


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
    assert "DestinationIdentityBindings" not in router.calls
    assert router.calls.count("IntentSemanticOwnershipReview") == 1


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
    contract = await _interpreter(router).interpret(SimpleNamespace(), [], "Иду наружу.")
    assert contract.clarification_required
    assert contract.actions == []


@pytest.mark.asyncio
async def test_unselected_endpoint_is_clarified_instead_of_binding_a_known_place():
    street_id = str(uuid4())
    player_input = "Иду наружу с рынка, но конкретную улицу пока не выбираю."
    router = _Router(
        {
            "summary": "Выхожу с рынка.",
            "actions": [{
                "action_type": "movement", "intent": "Выйти с рынка.",
                "destination_location": "улица",
            }],
        },
        review={
            "action_ownership": [{
                "action_index": 0,
                "actor_role": "speaker",
                "contribution_kind": "world_action",
                "action_type": "movement",
                "spatial_effect": "travel",
                "destination_location": None,
                "destination_reference_mode": None,
                "destination_committed": False,
            }],
            "information_request_only": False,
            "information_recipient": "none",
        },
    )
    result = await _interpreter(router).interpret(
        SimpleNamespace(), [], player_input,
        location_references={street_id: "улица"},
    )
    assert result.clarification_required
    assert result.actions == []
    assert "DestinationIdentityBindings" not in router.calls


@pytest.mark.asyncio
async def test_semantic_review_cannot_commit_endpoint_left_open_by_extraction():
    market_id = str(uuid4())
    router = _Router(
        {
            "summary": "Иду с рынка, но конкретное направление не определено.",
            "actions": [{
                "action_type": "movement",
                "intent": "Иду с рынка.",
                "destination_location": "рынок",
                "destination_committed": False,
            }],
        },
        review={
            "action_ownership": [{
                "action_index": 0,
                "actor_role": "speaker",
                "contribution_kind": "world_action",
                "action_type": "movement",
                "spatial_effect": "travel",
                "destination_location": "рынок",
                "destination_reference_mode": "explicit",
                "destination_committed": True,
            }],
            "information_request_only": False,
            "information_recipient": "none",
        },
        bindings={"action_0": market_id},
    )

    result = await _interpreter(router).interpret(
        SimpleNamespace(), [], "Иду наружу с рынка, но конкретную улицу пока не выбираю.",
        location_references={market_id: "Рыбный рынок"},
    )

    assert result.clarification_required
    assert result.actions == []
    assert "DestinationIdentityBindings" not in router.calls


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

    async def generate_json(
        self,
        provider,
        selection,
        messages,
        *,
        response_model,
        **kwargs,
    ):
        del provider, selection, kwargs
        self.calls.append(response_model.__name__)
        if response_model.__name__ == "DestinationIdentityBindings":
            return self.bindings
        if response_model.__name__ == "IntentSemanticOwnershipReview":
            human_input = (
                messages[-1]
                .content.split("[LATEST HUMAN INPUT]\n", 1)[1]
                .split("\n\n[EXTRACTED ACTIONS", 1)[0]
            )
            default_review = {
                "action_ownership": [
                    {
                        "action_index": index,
                        "actor_role": action.get("actor_role", "speaker"),
                        "evidence_quote": human_input,
                    }
                    for index, action in enumerate(self.payload.get("actions", []))
                ],
                "information_request_only": self.payload.get("information_request_only", False),
                "information_recipient": (
                    "narrator"
                    if self.payload.get("world_state_question", False)
                    else "character"
                    if self.payload.get("addressed_response_requested", False)
                    and self.payload.get("information_request_only", False)
                    else "none"
                ),
                "addressed_character_name": self.payload.get("addressed_character_name"),
            }
            return self.review or default_review
        assert issubclass(response_model, PlayerIntentContractDraft)
        return self.payload


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


@pytest.mark.asyncio
async def test_disputed_inventory_act_is_reconsidered_before_being_erased():
    item_id, npc_id = str(uuid4()), str(uuid4())
    payload = {"summary": "Передаю ключ.", "actions": [{
        "action_type": "inventory", "intent": "Передаю ключ.", "actor_role": "speaker",
        "item_id": item_id, "inventory_operation": "give", "inventory_target_id": npc_id,
    }]}
    mistaken = {
        "action_ownership": [{"action_index": 0, "actor_role": "speaker",
                              "contribution_kind": "speech", "evidence_quote": "Передаю ключ."}],
        "information_request_only": True, "information_recipient": "narrator",
    }

    class DisputingRouter(_Router):
        async def generate_json(self, *args, response_model, **kwargs):
            if response_model.__name__ == "IntentSemanticOwnershipReview":
                if self.calls.count("IntentSemanticOwnershipReview"):
                    self.review = {
                        "action_ownership": [{"action_index": 0, "actor_role": "speaker",
                                              "contribution_kind": "world_action",
                                              "spatial_effect": "local",
                                              "evidence_quote": "Передаю ключ."}],
                        "information_request_only": False, "information_recipient": "none",
                    }
            return await super().generate_json(*args, response_model=response_model, **kwargs)

    router = DisputingRouter(payload, review=mistaken)
    result = await _interpreter(router).interpret(SimpleNamespace(), [], "Передаю ключ.")
    assert result.actions[0].inventory_operation == "give"
    assert str(result.actions[0].item_id) == item_id
    assert router.calls.count("IntentSemanticOwnershipReview") == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("effect", ["inventory", "rest"])
async def test_semantic_effect_review_recovers_durable_operation_from_generic_extraction(effect):
    item_id = str(uuid4())
    fields = ({"item_id": item_id, "inventory_operation": "place"} if effect == "inventory"
              else {"elapsed_time": "восемь часов", "time_after": "утро"})
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
async def test_invented_actor_evidence_cannot_authorize_an_action():
    router = _Router({
        "summary": "Прошу указать улицу, затем жду рассвета.",
        "actions": [{"action_type": "wait", "intent": "Жду рассвета.",
                     "actor_role": "addressee", "time_after": "Рассвет"}],
    }, review={
        "action_ownership": [{"action_index": 0, "actor_role": "addressee",
                              "action_type": "wait", "time_after": "Рассвет",
                              "evidence_quote": "пересказ вместо цитаты"}],
        "information_request_only": False, "information_recipient": "none",
    })
    with pytest.raises((ValueError, TurnPlanningError), match="exact span"):
        await _interpreter(router).interpret(
            SimpleNamespace(), [], "Прошу указать улицу, затем жду рассвета.")


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


def test_information_summary_cannot_erase_typed_physical_action_in_mixed_turn():
    review = _intent_semantic_review_wire(2, "Осматриваю дверь и прошу объяснений.").model_validate(
        {
            "action_ownership": [
                {"action_index": 0, "actor_role": "speaker", "contribution_kind": "world_action"},
                {"action_index": 1, "actor_role": "addressee", "contribution_kind": "speech"},
            ],
            "information_request_only": True,
            "information_recipient": "character",
        }
    )
    assert review.information_request_only is False


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
    assert router.calls == [
        "PlayerIntentContractDraft",
        "IntentSemanticOwnershipReview",
        "DestinationIdentityBindings",
    ]


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
    assert "DestinationIdentityBindings" in router.calls


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
    assert "DestinationIdentityBindings" in router.calls


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
    with pytest.raises(TurnPlanningError, match="player intent interpretation failed"):
        await _interpreter(router).interpret(
            SimpleNamespace(),
            [],
            "Иду в комнату.",
            location_references={str(uuid4()): "Комната"},
        )


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
    assert router.calls == [
        "PlayerIntentContractDraft", "IntentSemanticOwnershipReview", "DestinationIdentityBindings",
    ]


@pytest.mark.asyncio
async def test_healthy_intent_path_uses_one_semantic_control_call() -> None:
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
        }
    )
    interpreter = _interpreter(router)

    result = await interpreter.interpret(
        SimpleNamespace(),
        [ChatMessage(role="system", content="AUTHORITATIVE STATE")],
        "Я выхожу в коридор.",
        location_references={str(uuid4()): "Коридор"},
    )

    assert result.actions[0].destination_location == "Коридор"
    assert router.calls == ["PlayerIntentContractDraft", "IntentSemanticOwnershipReview"]
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


def test_true_movement_without_destination_still_fails_closed() -> None:
    draft = PlayerIntentContractDraft.model_validate(
        {
            "summary": "Кай куда-то идёт.",
            "actions": [{"action_type": "movement", "intent": "Иду дальше."}],
        }
    )

    with pytest.raises(TurnPlanningError, match="missing the player-selected destination"):
        normalize_intent_draft(draft, "Иду дальше.")


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


def test_actor_role_is_required_on_the_model_schema() -> None:
    schema = _IntentWire.model_json_schema()
    action_refs = [item["$ref"] for item in schema["properties"]["actions"]["items"]["anyOf"]]
    for ref in action_refs:
        name = ref.rsplit("/", 1)[-1]
        definition = schema["$defs"][name]
        assert "actor_role" in definition["properties"]
        assert "actor_role" in definition["required"]


def test_semantic_ownership_review_requires_literal_actor_evidence() -> None:
    wire = _intent_semantic_review_wire(1, "Мария, закрой дверь.")
    payload = {
        "action_ownership": [
            {
                "action_index": 0,
                "actor_role": "addressee",
                "evidence_quote": "Мария, закрой дверь",
            }
        ],
        "information_request_only": False,
        "information_recipient": "none",
        "addressed_character_name": "Мария",
    }

    assert wire.model_validate(payload).action_ownership[0].actor_role == "addressee"
    payload["action_ownership"][0]["evidence_quote"] = "придуманная цитата"
    with pytest.raises(ValueError, match="exact span"):
        wire.model_validate(payload)
    payload["action_ownership"].append(dict(payload["action_ownership"][0]))
    with pytest.raises(ValueError):
        wire.model_validate(payload)


@pytest.mark.asyncio
async def test_empty_action_turn_skips_ownership_model_and_empty_literal() -> None:
    router = _Router({"summary": "Я киваю.", "actions": []})

    result = await _interpreter(router).interpret(SimpleNamespace(), [], "Я киваю.")

    assert result.actions == []
    assert router.calls == ["PlayerIntentContractDraft"]


@pytest.mark.asyncio
async def test_inconsistent_recipient_with_literal_quote_does_not_abort_turn() -> None:
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
                    "evidence_quote": "открываю дверь",
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
async def test_direct_speech_singular_command_is_owned_by_addressee() -> None:
    player_input = "Раздевайся. — Говорю я."
    router = _Router(
        {
            "summary": "Виктор раздевается.",
            "actions": [
                {
                    "action_type": "other",
                    "actor_role": "speaker",
                    "intent": "раздеться",
                }
            ],
            "addressed_character_name": "Мария",
        },
        review={
            "action_ownership": [
                {
                    "action_index": 0,
                    "actor_role": "speaker",
                    "evidence_quote": "Говорю я.",
                }
            ],
            "information_request_only": False,
            "information_recipient": "none",
            "addressed_character_name": "Мария",
        },
    )

    result = await _interpreter(
        router,
        LinguisticIntentAnalysis(imperative_clauses=("Раздевайся.",)),
    ).interpret(
        SimpleNamespace(),
        [],
        player_input,
    )

    assert result.summary == "Раздевайся. — Говорю я."
    assert result.actions[0].action_type == "service"
    assert result.addressed_response_requested is True
    assert result.addressed_character_name == "Мария"


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
            "action_type": "movement", "intent": "Вернуться на причал.",
            "destination_location": selected,
        }]},
        bindings={"action_0": location_id},
    )
    result = await PlayerIntentInterpreter(router).interpret(
        SimpleNamespace(), [], f"Возвращаюсь на {selected}.",
        location_references={location_id: persisted},
    )
    assert result.actions[0].destination_location == persisted
    assert "DestinationIdentityBindings" in router.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("substitute", [False, True])
async def test_explicit_new_destination_cannot_bind_to_an_unrelated_catalog_entry(substitute):
    office_id = str(uuid4())
    router = _Router(
        {"summary": "Выйти из здания и дойти до прачечной.", "actions": [{
            "action_type": "movement", "intent": "Выйти из здания.",
            "destination_location": "наружу",
        }]},
        bindings={"action_0": office_id if substitute else "new"},
        review={
            "action_ownership": [{
                "action_index": 0, "actor_role": "speaker", "contribution_kind": "world_action",
                "action_type": "movement", "spatial_effect": "travel",
                "destination_location": "круглосуточная прачечная соседнего дома",
                "destination_reference_mode": "explicit",
            }],
            "information_request_only": False, "information_recipient": "none",
        },
    )
    interpreter = _interpreter(router)
    result = await interpreter.interpret(
        SimpleNamespace(), [], "Выхожу из здания и иду в прачечную соседнего дома.",
        location_references={office_id: "Контора"},
    )
    assert result.actions[0].destination_location == "круглосуточная прачечная соседнего дома"
    assert "DestinationIdentityBindings" not in router.calls
