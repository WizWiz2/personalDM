"""The prose validator checks the four bans using compact sentence anchors."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

pytestmark = pytest.mark.interagent_contract_enforced

from app.models.item_state import ItemStateSnapshot
from app.models.turn_authority import TurnAuthority
from app.services.narration_publication_guard import NarrationPublicationGuard
from app.services.turn_authority_validator import TurnAuthorityValidator

TEXT = "Две монеты лежат на стойке. Борм кивает."


def _authority():
    hero = uuid4()
    return TurnAuthority(campaign_id=uuid4(), trigger_turn_id=uuid4(), player_input="Плачу.",
                         item_states=[ItemStateSnapshot(item_id=uuid4(), name="Две монеты", owner_id=hero)])


@pytest.mark.asyncio
async def test_validator_wire_has_only_violations_over_four_prose_bans():
    router = SimpleNamespace(generate_json=AsyncMock(return_value={"violations": [], "names_given": []}))
    auth = _authority()
    result = await TurnAuthorityValidator(router).validate(SimpleNamespace(), auth, TEXT)
    kwargs = router.generate_json.await_args.kwargs
    schema = kwargs["response_model"].model_json_schema()
    assert set(schema["properties"]) <= {"violations", "names_given", "non_story_sentences"}
    found = schema["$defs"][schema["properties"]["violations"]["items"]["$ref"].split("/")[-1]]
    assert set(found["properties"]["violation_type"]["enum"]) == {
        "absent_character", "canon_conflict", "player_agency", "invalid_movement",
    }
    assert result.verdict == "pass"
    # A passing check publishes the prose verbatim: item state is owned by the executor, not certified by prose.
    text, publication = NarrationPublicationGuard.publish(auth, TEXT, result)
    assert text == TEXT and publication["mode"] == "validated_candidate"



@pytest.mark.asyncio
async def test_unauthorized_movement_requires_repair():
    candidate = "Борм ушёл в соседнюю комнату."
    router = SimpleNamespace(generate_json=AsyncMock(return_value={
        "violations": [{"violation_type": "invalid_movement", "sentence": "S0"}],
        "names_given": [],
    }))
    auth = _authority().model_copy(update={"present_character_names": ["Борм"]})
    result = await TurnAuthorityValidator(router).validate(SimpleNamespace(), auth, candidate)
    assert result.verdict == "repair_required"
    assert result.violations[0].evidence == candidate


@pytest.mark.asyncio
async def test_registered_alias_is_valid_only_for_its_own_cast_member():
    auth = _authority().model_copy(update={
        "present_character_names": ["Йонас Кель", "Борм"],
        "character_aliases": {"Йонас Кель": ["Господин Кель"]},
    })
    candidate = "Господин Кель кивает."
    generate = AsyncMock(return_value={
        "violations": [],
        "names_given": [{"who": "Йонас Кель", "name": "Господин Кель", "sentence": "S0"}],
    })
    validator = TurnAuthorityValidator(SimpleNamespace(generate_json=generate))
    assert (await validator.validate(SimpleNamespace(), auth, candidate)).verdict == "pass"
    generate.return_value["names_given"][0]["who"] = "Борм"
    assert (await validator.validate(SimpleNamespace(), auth, candidate)).verdict == "repair_required"


@pytest.mark.asyncio
async def test_story_context_distinguishes_a_character_line_from_a_writer_note():
    text = "Хроника не ждёт вдохновения."
    generate = AsyncMock(return_value={"non_story_sentences": ["S0"]})
    validator = TurnAuthorityValidator(SimpleNamespace(generate_json=generate))
    note = await validator.validate(SimpleNamespace(), _authority(), text)
    assert note.verdict == "repair_required"
    assert note.violations[0].violation_type == "meta_language"
    assert note.violations[0].evidence == text

    generate.return_value = {"non_story_sentences": []}
    speech = f"— {text} — сказал летописец."
    story = await validator.validate(SimpleNamespace(), _authority(), speech)
    assert NarrationPublicationGuard.publish(_authority(), speech, story)[0] == speech
