from uuid import uuid4

from app.models.turn import ChatMessage
from app.services.player_intent_interpreter import (
    PlayerActionIntentDraft,
    PlayerIntentContractDraft,
    _repair_unambiguous_local_placement,
    normalize_intent_draft,
)


def _draft(text: str):
    return PlayerIntentContractDraft(
        summary=text,
        actions=[PlayerActionIntentDraft(action_type="interaction", intent=text)],
    )


def _context(owned="", floor=""):
    return [ChatMessage(role="system", content=(
        f"Player-owned items: {owned}\nObjects physically here: {floor}"
    ))]


def test_recover_qa213_letter_placement_from_interaction_label():
    letter = uuid4()
    draft = _draft("Укладываю письмо между лампой и стеной.")
    assert _repair_unambiguous_local_placement(
        draft, draft.summary,
        _context(owned=f"письмо [id={letter}]"),
    )
    action = normalize_intent_draft(draft, draft.summary).actions[0]
    assert action.action_type == "inventory"
    assert action.inventory_operation == "place"
    assert action.item_id == letter
    assert action.item_placement_detail == "между лампой и стеной"


def test_recover_qa213_coin_reposition_with_russian_case_ending():
    coin = uuid4()
    draft = _draft("Ставлю монету под каблук.")
    assert _repair_unambiguous_local_placement(
        draft, draft.summary,
        _context(floor=f"монета [id={coin}]"),
    )
    action = normalize_intent_draft(draft, draft.summary).actions[0]
    assert action.inventory_operation == "reposition"
    assert action.item_placement_detail == "под каблук"


def test_ambiguous_item_or_carried_container_is_not_invented():
    a, b = uuid4(), uuid4()
    ambiguous = _draft("Кладу ключ на стол.")
    context = _context(owned=f"старый ключ [id={a}], запасной ключ [id={b}]")
    assert not _repair_unambiguous_local_placement(ambiguous, ambiguous.summary, context)
    assert ambiguous.actions[0].action_type == "interaction"
    carried = _draft("Убираю письмо в сумку.")
    assert _repair_unambiguous_local_placement(
        carried, carried.summary, _context(owned=f"письмо [id={a}]")
    )
    result = normalize_intent_draft(carried, carried.summary).actions[0]
    assert result.item_id == a
    assert result.inventory_operation == "reposition"
    assert result.item_placement_detail == "в сумку"
    floor = _draft("Убираю письмо в сумку.")
    assert not _repair_unambiguous_local_placement(
        floor, floor.summary, _context(floor=f"письмо [id={a}]")
    )


def test_a_negative_instruction_does_not_move_items():
    letter = uuid4()
    text = "Не кладу письмо на стол."
    draft = _draft(text)
    assert not _repair_unambiguous_local_placement(
        draft, text, _context(owned=f"письмо [id={letter}]")
    )


def test_enrich_typed_place_that_forgot_surface():
    letter = uuid4()
    text = "Кладу письмо между лампой и стеной."
    draft = PlayerIntentContractDraft(
        summary=text,
        actions=[PlayerActionIntentDraft(
            action_type="inventory", intent=text, item_id=str(letter),
            inventory_operation="place", item_placement_detail=None,
        )],
    )
    assert _repair_unambiguous_local_placement(
        draft, text, _context(owned=f"письмо [id={letter}]"),
    )
    action = normalize_intent_draft(draft, text).actions[0]
    assert action.item_id == letter
    assert action.inventory_operation == "place"
    assert action.item_placement_detail == "между лампой и стеной"
