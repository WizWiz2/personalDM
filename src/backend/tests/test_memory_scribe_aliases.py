from uuid import uuid4

from app.models.proposed_change import ChangeType
from app.services.memory_scribe import MemoryScribe


def setup_entities():
    actor = uuid4()
    player = uuid4()
    aria = uuid4()
    location = uuid4()
    known_ids = {str(actor), str(player), str(aria), str(location)}
    known = {
        "garrick": str(actor),
        "eldon": str(player),
        "aria": str(aria),
        "ruined chapel": str(location),
    }
    return actor, player, aria, location, known, known_ids


def test_placeholder_references_are_resolved_by_backend():
    actor, player, _, _, known, known_ids = setup_entities()
    scribe = MemoryScribe(None)

    assert scribe._resolve_reference(
        "SELF", known, known_ids, actor, player
    ) == str(actor)
    assert scribe._resolve_reference(
        "USER_CHARACTER_ID", known, known_ids, actor, player
    ) == str(player)
    assert scribe._resolve_reference(
        "Garrick", known, known_ids, actor, player
    ) == str(actor)


def test_embedded_known_uuid_is_recovered_but_unknown_uuid_is_rejected():
    actor, player, aria, _, known, known_ids = setup_entities()
    scribe = MemoryScribe(None)

    assert scribe._resolve_reference(
        f"aria{aria}", known, known_ids, actor, player
    ) == str(aria)
    assert scribe._resolve_reference(
        str(uuid4()), known, known_ids, actor, player
    ) is None


def test_direct_npc_knowledge_goes_to_player_not_speaker():
    actor, player, _, _, known, known_ids = setup_entities()
    scribe = MemoryScribe(None)
    payload = scribe._normalize_payload(
        ChangeType.KNOWLEDGE,
        {
            "recipient_id": "SELF",
            "source_character_id": "SELF",
            "proposition": "Гаррик сообщил, что западная дорога перекрыта.",
            "confidence": 0,
        },
        known,
        known_ids,
        actor,
        player,
        [str(actor), str(player)],
    )

    assert payload is not None
    assert payload["recipient_id"] == str(player)
    assert payload["source_character_id"] == str(actor)
    assert payload["confidence"] == 0.2


def test_public_event_expands_witnesses_and_discards_unknown_names():
    actor, player, aria, location, known, known_ids = setup_entities()
    scribe = MemoryScribe(None)
    payload = scribe._normalize_payload(
        ChangeType.EVENT,
        {
            "event_type": "предупреждение",
            "description": "В часовне прозвучал сигнал тревоги.",
            "location_id": "Ruined Chapel",
            "participant_ids": ["all", "Unknown Person"],
        },
        known,
        known_ids,
        actor,
        player,
        [str(actor), str(player), str(aria)],
    )

    assert payload is not None
    assert payload["location_id"] == str(location)
    assert payload["participant_ids"] == [str(actor), str(player), str(aria)]


def test_cjk_or_missing_knowledge_is_not_persisted():
    actor, player, _, _, known, known_ids = setup_entities()
    scribe = MemoryScribe(None)
    payload = scribe._normalize_payload(
        ChangeType.KNOWLEDGE,
        {
            "recipient_id": "Eldon",
            "source_character_id": "Garrick",
            "proposition": "未知的秘密",
            "confidence": 0.9,
        },
        known,
        known_ids,
        actor,
        player,
        [str(actor), str(player)],
    )
    assert payload is None
