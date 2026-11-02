"""A colliding role cannot become a new NPC merely by appending a number."""
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.services.turn_outcome_resolver import _outcome_wire_model


def _npc():
    return {"canonical_name": "Станционный оператор", "cast_id": "newcomer",
        "role": "станционный оператор", "temporary_name": True,
        "description": "Станционный работник в аккуратном потёртом кителе, рядом с телеграфом.",
        "appearance": "Мужчина среднего возраста в потёртом кителе, с пятнами краски на пальцах.",
        "reason": "Оператор отвечает на вопрос игрока."}


@pytest.mark.parametrize("caption", ["Станционный оператор", "Станционный оператор 2"])
def test_newcomer_cannot_reallocate_an_existing_cast_role(caption):
    person = str(uuid4())
    wire = _outcome_wire_model(0, known_cast={person: "Станционный оператор"})
    with pytest.raises(ValidationError, match="existing cast"):
        wire.model_validate({"action_outcomes": [], "observable_consequences": ["Собеседник отвечает."], "npc_introductions": [
            {**_npc(), "canonical_name": caption}]})
    bound = wire.model_validate({"action_outcomes": [], "observable_consequences": ["Собеседник отвечает."], "npc_introductions": [
        {**_npc(), "cast_id": person}]})
    assert bound.npc_introductions[0].cast_id == person
    assert bound.npc_introductions[0].canonical_name == "Станционный оператор"



def test_a_distinct_evidenced_person_can_share_the_existing_persons_profession():
    wire = _outcome_wire_model(0, known_cast={str(uuid4()): "Станционный оператор"})
    draft = wire.model_validate({"action_outcomes": [], "observable_consequences": ["Собеседник отвечает."], "npc_introductions": [
        {**_npc(), "canonical_name": "Илья", "temporary_name": False,
         "personal_name_evidence": "Меня зовут Илья."}]})
    assert draft.npc_introductions[0].cast_id == "newcomer"
    assert draft.npc_introductions[0].canonical_name == "Илья"
