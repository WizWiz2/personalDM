"""An in-turn return addresses a prior departure, not a similar catalog label."""
import pytest

from app.db.repositories.location_repo import LocationRepository
from app.db.tables import Character
from app.models.location import LocationCreate
from app.models.scene_state import LocationExitCreate
from app.services.player_intent_interpreter import PlayerIntentContractDraft, normalize_intent_draft
from app.services.scene_state_service import SceneStateService
from tests.test_qa219_regressions import _world
from tests.test_qa223_regressions import _execute


@pytest.mark.asyncio
async def test_compound_return_uses_the_original_departure_even_with_a_wrong_catalog_hint(db_session):
    cid, start, gallery, hero, _, _, scene = await _world(db_session)
    sea = await LocationRepository(db_session).create(cid, LocationCreate(canonical_name="Море"))
    service = SceneStateService(db_session)
    for a, b in [(start, gallery), (gallery, sea.id)]:
        await service.create_exit(cid, a, LocationExitCreate(to_location_id=b, label="Проход", bidirectional=True))
    text = "Выхожу на галерею, спускаюсь к морю, затем возвращаюсь туда, откуда вышел."
    actions = [
        {"action_type": "movement", "intent": "Выйти на галерею.", "destination_location": "Галерея", "place": str(gallery)},
        {"action_type": "movement", "intent": "Спуститься к морю.", "destination_location": "Море", "place": str(sea.id)},
        # Archived failure selected a service location. A typed departure reference owns the return.
        {"action_type": "movement", "intent": "Вернуться в исходный зал.", "destination_location": "Галерея",
         "place": str(gallery), "return_to_action_index": 0},
    ]
    contract = normalize_intent_draft(PlayerIntentContractDraft.model_validate({"summary": text, "actions": actions}), text)
    from app.services.action_plan_compiler import ActionPlanCompiler

    decision = await ActionPlanCompiler(db_session).resolve_known_travel(cid, contract)
    assert decision is not None
    execution = await _execute(db_session, cid, scene, contract, decision)
    assert [step.status for step in execution.steps] == ["completed"] * 3
    assert (await db_session.get(Character, str(hero))).current_location_id == str(start)


def test_native_return_selects_an_earlier_departure_without_a_catalog_destination():
    from uuid import uuid4

    from pydantic import ValidationError

    from app.services.player_intent_interpreter import PlayerIntentInterpreter, _intent_wire_model

    actor, start, gallery = map(str, (uuid4(), uuid4(), uuid4()))
    wire = _intent_wire_model([], {start: "Зал", gallery: "Галерея"}, (gallery,))
    first = {"evidence_quote": "выхожу", "contribution_kind": "world_action", "actor_id": actor,
             "intent": "Выйти на галерею.", "action_type": "movement", "spatial_effect": "travel",
             "destination_location": "Галерея", "place": gallery,
             "returns_to_earlier_scene": False, "movement_method": "ordinary"}
    back = {"evidence_quote": "возвращаюсь", "contribution_kind": "world_action", "actor_id": actor,
            "intent": "Вернуться в исходный зал.", "action_type": "movement",
            "return_to_action_index": 0, "movement_method": "ordinary"}
    result = wire.model_validate({"summary": "Выхожу и возвращаюсь.", "actions": [first, back]})
    draft = PlayerIntentContractDraft.model_validate(result.model_dump())
    PlayerIntentInterpreter._check_places(draft, {start: "Зал", gallery: "Галерея"})
    contract = normalize_intent_draft(draft, "Выхожу и возвращаюсь.")
    assert contract.actions[1].action_type == "movement"
    assert contract.actions[1].return_to_action_index == 0
    assert contract.actions[1].destination_location_id is None
    with pytest.raises(ValidationError, match="earlier movement"):
        wire.model_validate({"summary": "Возвращаюсь.", "actions": [back]})
