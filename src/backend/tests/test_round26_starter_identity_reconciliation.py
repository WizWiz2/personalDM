from types import SimpleNamespace
from uuid import uuid4

from app.models.session_zero_interview import SessionZeroInterviewState, SessionZeroStarterNPC
from app.services.entity_identity import resolve_character_candidates
from app.services.playable_bootstrap import PlayableBootstrap


def test_round26_explicit_irina_is_recovered_from_structured_description():
    npc = SessionZeroStarterNPC(
        role="Судебный фотограф",
        name=None,
        description="Ирина — судебный фотограф, которая пришла лично поговорить с Виктором.",
        present_at_start=True,
    )

    assert npc.name == "Ирина"
    assert PlayableBootstrap._starter_name(npc) == "Ирина"


def test_role_followed_by_explicit_name_is_preserved():
    npc = SessionZeroStarterNPC(
        role="судебный фотограф",
        description="Судебный фотограф Ирина пришла с материалами дела.",
    )

    assert npc.name == "Ирина"


def test_generic_description_does_not_invent_a_name():
    npc = SessionZeroStarterNPC(
        role="владелица книжной лавки",
        description="Нервная клиентка, пришедшая лично сообщить о пропаже.",
    )

    assert npc.name is None
    assert PlayableBootstrap._starter_name(npc) == "владелица книжной лавки"


def test_nested_session_zero_state_applies_starter_identity_normalization():
    state = SessionZeroInterviewState.model_validate(
        {
            "draft": {
                "world": {
                    "starter_presence_confirmed": True,
                    "starter_npcs": [
                        {
                            "role": "Судебный фотограф",
                            "name": None,
                            "description": "Ирина: судебный фотограф и свидетельница по делу.",
                        }
                    ],
                },
                "character": {},
            }
        }
    )

    assert state.draft.world.starter_npcs[0].name == "Ирина"


def _starter(entity_id, location_id, *, role="Судебный фотограф"):
    return SimpleNamespace(
        id=entity_id,
        canonical_name=role,
        aliases=[],
        description="Временная starter identity",
        custom_fields={
            "source": "session_zero_structured_presence",
            "temporary_name": True,
            "role": role,
            "bootstrap_role": role,
        },
    ), location_id


def test_named_npc_reuses_unique_same_location_structured_starter_role():
    entity_id = uuid4()
    location_id = uuid4()
    starter, location = _starter(entity_id, location_id)

    matches = resolve_character_candidates(
        [starter],
        proposed_name="Ирина",
        proposed_role="Судебный фотограф",
        temporary_name=False,
        target_location_id=location_id,
        character_locations={entity_id: location},
    )

    assert [item.id for item in matches] == [entity_id]


def test_structured_starter_role_reconciliation_is_same_location_only():
    entity_id = uuid4()
    old_location = uuid4()
    target_location = uuid4()
    starter, _ = _starter(entity_id, old_location)

    matches = resolve_character_candidates(
        [starter],
        proposed_name="Ирина",
        proposed_role="Судебный фотограф",
        temporary_name=False,
        target_location_id=target_location,
        character_locations={entity_id: old_location},
    )

    assert matches == []


def test_ambiguous_temporary_starters_remain_ambiguous_and_fail_closed_upstream():
    location_id = uuid4()
    first_id = uuid4()
    second_id = uuid4()
    first, _ = _starter(first_id, location_id)
    second, _ = _starter(second_id, location_id)

    matches = resolve_character_candidates(
        [first, second],
        proposed_name="Ирина",
        proposed_role="Судебный фотограф",
        temporary_name=False,
        target_location_id=location_id,
        character_locations={first_id: location_id, second_id: location_id},
    )

    assert {item.id for item in matches} == {first_id, second_id}
