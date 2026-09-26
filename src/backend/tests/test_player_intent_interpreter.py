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
            human_input = messages[-1].content.split("[LATEST HUMAN INPUT]\n", 1)[1].split(
                "\n\n[EXTRACTED ACTIONS", 1
            )[0]
            default_review = {
                "action_ownership": [
                    {
                        "action_index": index,
                        "actor_role": action.get("actor_role", "speaker"),
                        "evidence_quote": human_input,
                    }
                    for index, action in enumerate(self.payload.get("actions", []))
                ],
                "information_request_only": self.payload.get(
                    "information_request_only", False
                ),
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


def _interpreter(
    router: _Router,
    analysis: LinguisticIntentAnalysis | None = None,
) -> PlayerIntentInterpreter:
    return PlayerIntentInterpreter(router, linguistic_analyzer=_LinguisticAnalyzer(analysis))


@pytest.mark.asyncio
async def test_imperative_information_request_is_speech_despite_addressee_syntax():
    player_input = "Назови себя и объясни, откуда знаешь моё имя."
    router = _Router({
        "summary": "Прошу объяснений.",
        "actions": [{"action_type": "service", "intent": player_input, "actor_role": "addressee"}],
        "addressed_response_requested": True,
        "addressed_character_name": "Контактное лицо",
    }, review={
        "action_ownership": [{"action_index": 0, "actor_role": "addressee",
                              "contribution_kind": "speech", "evidence_quote": player_input}],
        "information_request_only": True, "information_recipient": "character",
        "addressed_character_name": "Контактное лицо",
    })
    result = await _interpreter(router, LinguisticIntentAnalysis(
        uniform_action_role="addressee", action_roles=("addressee",),
        imperative_clauses=(player_input,),
    )).interpret(SimpleNamespace(), [], player_input)
    assert result.actions == []
    assert result.addressed_response_requested is True
    assert result.addressed_character_name == "Контактное лицо"
    assert result.world_state_question is False


@pytest.mark.asyncio
async def test_mixed_turn_removes_only_speech_candidate_and_preserves_physical_act():
    router = _Router({
        "summary": "Осматриваю раму и спрашиваю дежурного.",
        "actions": [
            {"action_type": "observation", "intent": "Осматриваю раму."},
            {"action_type": "service", "intent": "Объясни, кто закрыл дверь."},
        ],
        "addressed_response_requested": True, "addressed_character_name": "Дежурный",
    }, review={
        "action_ownership": [
            {"action_index": 0, "actor_role": "speaker", "contribution_kind": "world_action"},
            {"action_index": 1, "actor_role": "addressee", "contribution_kind": "speech"},
        ],
        "information_request_only": False, "information_recipient": "none",
    })
    result = await _interpreter(router).interpret(
        SimpleNamespace(), [], "Осматриваю раму. Дежурный, объясни, кто закрыл дверь.",
    )
    assert [(act.action_type, act.intent) for act in result.actions] == [
        ("observation", "Осматриваю раму."),
    ]
    assert result.addressed_response_requested is True


def test_long_dialogue_preserves_contract_without_overflowing_summary():
    input_text = "Объясни, откуда знаешь моё имя. " * 30
    draft = PlayerIntentContractDraft.model_validate({
        "summary": "Прошу рассказать, откуда собеседник знает моё имя.",
        "actions": [], "information_request_only": True, "addressed_response_requested": True,
    })
    contract = normalize_intent_draft(draft, input_text)
    assert contract.actions == []
    assert contract.addressed_response_requested is True
    assert len(contract.summary) <= 500


def test_information_summary_cannot_erase_typed_physical_action_in_mixed_turn():
    review = _intent_semantic_review_wire(2, "Осматриваю дверь и прошу объяснений.").model_validate({
        "action_ownership": [
            {"action_index": 0, "actor_role": "speaker", "contribution_kind": "world_action"},
            {"action_index": 1, "actor_role": "addressee", "contribution_kind": "speech"},
        ],
        "information_request_only": True, "information_recipient": "character",
    })
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
    router = _Router({
        "summary": "Возвращаюсь в свою комнату.",
        "actions": [{
            "action_type": "movement",
            "intent": "Вернуться в свою комнату.",
            "destination_location": "свою комнату",
        }],
    })
    interpreter = PlayerIntentInterpreter(router)

    result = await interpreter.interpret(
        SimpleNamespace(), [], "Я возвращаюсь из коридора в свою комнату.",
        location_references={
            room_id: "Комната Кая",
            surroundings_id: "Окрестности — Комната Кая",
        },
    )

    assert result.actions[0].destination_location == "Комната Кая"
    assert "DestinationIdentityBindings" not in router.calls


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
    assert router.calls == ["PlayerIntentContractDraft", "IntentSemanticOwnershipReview"]


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
    action_refs = [
        item["$ref"]
        for item in schema["properties"]["actions"]["items"]["anyOf"]
    ]
    for ref in action_refs:
        name = ref.rsplit("/", 1)[-1]
        definition = schema["$defs"][name]
        assert "actor_role" in definition["properties"]
        assert "actor_role" in definition["required"]


def test_semantic_ownership_review_keeps_coverage_but_tolerates_paraphrased_evidence() -> None:
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
    assert wire.model_validate(payload).action_ownership[0].evidence_quote == "придуманная цитата"
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
async def test_inconsistent_recipient_and_paraphrased_quote_do_not_abort_turn() -> None:
    router = _Router(
        {"summary": "Я открываю дверь.", "actions": [
            {"action_type": "interaction", "intent": "Открыть дверь."}
        ]},
        review={
            "action_ownership": [{
                "action_index": 0,
                "actor_role": "speaker",
                "evidence_quote": "Открываю дверь",
            }],
            "information_request_only": False,
            "information_recipient": "character",
            "addressed_character_name": None,
        },
    )

    result = await _interpreter(router).interpret(
        SimpleNamespace(), [], "Я открываю дверь."
    )

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
