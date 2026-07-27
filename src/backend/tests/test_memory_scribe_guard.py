from app.models.proposed_change import (
    ChangeType,
    ProposedChangeCreate,
)
from app.services.memory_scribe import MemoryScribe
from app.services.memory_scribe_guard import (
    _downgrade_sourced_knowledge_claim,
    _evidence_supports_description,
    _replace_player_meta_in_value,
)


def test_evidence_guard_rejects_relation_to_character_missing_from_quote():
    assert (
        _evidence_supports_description(
            {
                "description": (
                    "Единственное подозрительное в Маргарите Грей — "
                    "это аномалия у края локации."
                ),
                "evidence": (
                    "Единственная подозрительная вещь здесь — "
                    "это та аномалия у края."
                ),
            }
        )
        is False
    )


def test_gap_repair_preserves_evidence_when_outcome_description_mismatches():
    scribe = MemoryScribe(None)
    proposals = scribe._parse_data(
        {
            "outcomes": [
                {
                    "id": "event1",
                    "kind": "event",
                    "description": "Виктор сделал шаг и указал на жижу.",
                    "evidence": "Виктор повернул голову и выдержал паузу.",
                    "authority": "public_observation",
                    "durable": True,
                }
            ],
            "proposals": [],
        },
        authoritative_text=(
            "Виктор повернул голову и выдержал паузу."
        ),
        known_entities={},
        known_ids=set(),
        acting_character_id=None,
        player_character_id=None,
        scene_participant_ids=[],
    )

    assert len(proposals) == 1
    assert proposals[0].change_type == ChangeType.EVENT
    assert "повернул голову" in proposals[0].payload["description"]
    assert "сделал шаг" not in proposals[0].payload["description"]
    assert proposals[0].payload["_canon"]["_evidence_mismatch"] is True


def test_sourced_knowledge_is_downgraded_to_character_claim():
    proposal = ProposedChangeCreate(
        change_type=ChangeType.KNOWLEDGE,
        payload={
            "recipient_id": "eldon-id",
            "source_character_id": "victor-id",
            "proposition": "Кровь кающегося не реагирует на дождь.",
            "status": "known",
        },
    )
    canon_meta = {
        "kind": "knowledge_transfer",
        "authority": "public_observation",
    }

    _downgrade_sourced_knowledge_claim(proposal, canon_meta)

    assert canon_meta["authority"] == "character_claim"
    assert proposal.payload["status"] == "believed"
    assert proposal.payload["proposition"].startswith("Заявление источника:")


def test_existing_character_claim_is_also_stored_as_belief():
    proposal = ProposedChangeCreate(
        change_type=ChangeType.KNOWLEDGE,
        payload={
            "recipient_id": "eldon-id",
            "source_character_id": "elias-id",
            "proposition": "Жидкость содержит соединения серы.",
            "status": "known",
        },
    )
    canon_meta = {
        "kind": "knowledge_transfer",
        "authority": "character_claim",
    }

    _downgrade_sourced_knowledge_claim(proposal, canon_meta)

    assert proposal.payload["status"] == "believed"
    assert proposal.payload["proposition"] == (
        "Заявление источника: Жидкость содержит соединения серы."
    )


def test_sourced_knowledge_uses_exact_evidence_instead_of_distorted_paraphrase():
    proposal = ProposedChangeCreate(
        change_type=ChangeType.KNOWLEDGE,
        payload={
            "recipient_id": "eldon-id",
            "source_character_id": "victor-id",
            "proposition": "Маргариту следует изучить.",
            "status": "known",
        },
    )
    canon_meta = {
        "kind": "knowledge_transfer",
        "authority": "character_claim",
        "evidence": "Наш следующий шаг — очищение. Начать следует с Маргариты.",
    }

    _downgrade_sourced_knowledge_claim(proposal, canon_meta)

    assert proposal.payload["proposition"] == (
        "Заявление источника: Наш следующий шаг — очищение. "
        "Начать следует с Маргариты."
    )
    assert proposal.payload["status"] == "believed"


def test_mismatched_character_claim_is_repaired_as_belief_not_world_event():
    scribe = MemoryScribe(None)
    proposals = scribe._parse_data(
        {
            "outcomes": [
                {
                    "id": "claim1",
                    "kind": "knowledge_transfer",
                    "description": (
                        "Верана Грим передала Элдону сведения о направлении."
                    ),
                    "evidence": (
                        "Верана Грим сказала: «Роса гуще к востоку»."
                    ),
                    "authority": "character_claim",
                    "durable": True,
                }
            ],
            "proposals": [
                {
                    "outcome_id": "claim1",
                    "change_type": "event",
                    "operation": "assert",
                    "cardinality": "single",
                    "payload": {
                        "event_type": "narrative_event",
                        "description": "Источник находится на востоке.",
                    },
                }
            ],
        },
        authoritative_text=(
            "Верана Грим сказала: «Роса гуще к востоку»."
        ),
        known_entities={
            "Элдон": "eldon-id",
            "Верана Грим": "verana-id",
        },
        known_ids={"eldon-id", "verana-id"},
        acting_character_id="eldon-id",
        player_character_id="eldon-id",
        scene_participant_ids=["eldon-id", "verana-id"],
    )

    assert len(proposals) == 1
    assert proposals[0].change_type == ChangeType.KNOWLEDGE
    assert proposals[0].payload["recipient_id"] == "eldon-id"
    assert proposals[0].payload["source_character_id"] == "verana-id"
    assert proposals[0].payload["status"] == "believed"
    assert proposals[0].payload["proposition"].startswith(
        "Заявление источника:"
    )


def test_character_claim_source_comes_from_speaker_before_quote():
    scribe = MemoryScribe(None)
    authoritative = (
        "Верана Грим подняла глаза и сказала: "
        "«Если идти дальше, цена будет личной». "
        "Тарн молча выслушал её."
    )
    proposals = scribe._parse_data(
        {
            "outcomes": [
                {
                    "id": "claim1",
                    "kind": "knowledge_transfer",
                    "description": "Тарн узнал о личном риске.",
                    "evidence": (
                        "Если идти дальше, цена будет личной"
                    ),
                    "authority": "character_claim",
                    "durable": True,
                }
            ],
            "proposals": [],
        },
        authoritative_text=authoritative,
        known_entities={
            "Элдон": "eldon-id",
            "Верана Грим": "verana-id",
            "Тарн": "tarn-id",
        },
        known_ids={"eldon-id", "verana-id", "tarn-id"},
        acting_character_id="eldon-id",
        player_character_id="eldon-id",
        scene_participant_ids=["eldon-id", "verana-id", "tarn-id"],
    )

    assert len(proposals) == 1
    assert proposals[0].change_type == ChangeType.KNOWLEDGE
    assert proposals[0].payload["source_character_id"] == "verana-id"


def test_supported_movement_with_unknown_entity_becomes_narrative_event():
    scribe = MemoryScribe(None)
    proposals = scribe._parse_data(
        {
            "outcomes": [
                {
                    "id": "move1",
                    "kind": "movement",
                    "description": "Незнакомец вошёл в неизвестную башню.",
                    "evidence": "Незнакомец вошёл в неизвестную башню",
                    "authority": "dm_confirmed",
                    "durable": True,
                }
            ],
            "proposals": [
                {
                    "outcome_id": "move1",
                    "change_type": "movement",
                    "operation": "assert",
                    "cardinality": "single",
                    "payload": {
                        "character_id": "Незнакомец",
                        "location_id": "Неизвестная башня",
                        "description": "Незнакомец вошёл в неизвестную башню.",
                    },
                }
            ],
        },
        authoritative_text="Незнакомец вошёл в неизвестную башню.",
        known_entities={},
        known_ids=set(),
        acting_character_id=None,
        player_character_id=None,
        scene_participant_ids=[],
    )

    assert len(proposals) == 1
    assert proposals[0].change_type == ChangeType.EVENT
    assert proposals[0].payload["description"] == (
        "Незнакомец вошёл в неизвестную башню."
    )
    assert proposals[0].payload["_canon"]["preserved_as"] == "narrative_event"
    assert scribe.last_audit["envelope_valid"] is True
    assert scribe.last_audit["gap_count"] == 0
    assert scribe.last_audit["coverage_ratio"] == 1.0


def test_supported_event_gap_is_repaired_without_inventing_payload():
    scribe = MemoryScribe(None)
    proposals = scribe._parse_data(
        {
            "outcomes": [
                {
                    "id": "event1",
                    "kind": "event",
                    "description": "Валентин резко сменил тему разговора.",
                    "evidence": "Валентин резко сменил тему разговора",
                    "authority": "public_observation",
                    "durable": True,
                }
            ],
            "proposals": [],
        },
        authoritative_text="Валентин резко сменил тему разговора.",
        known_entities={},
        known_ids=set(),
        acting_character_id=None,
        player_character_id=None,
        scene_participant_ids=[],
    )

    assert len(proposals) == 1
    assert proposals[0].change_type == ChangeType.EVENT
    assert proposals[0].payload["description"] == "Валентин резко сменил тему разговора."
    assert scribe.last_audit["envelope_valid"] is True
    assert scribe.last_audit["gap_count"] == 0


def test_public_observation_gap_is_preserved_as_evidence_backed_event():
    scribe = MemoryScribe(None)
    proposals = scribe._parse_data(
        {
            "outcomes": [
                {
                    "id": "fact1",
                    "kind": "world_state",
                    "description": "Свет фонаря преломился в кристалле, и запах озона усилился.",
                    "evidence": "Свет фонаря преломился в кристалле, и запах озона усилился",
                    "authority": "public_observation",
                    "durable": True,
                }
            ],
            "proposals": [],
        },
        authoritative_text=(
            "Свет фонаря преломился в кристалле, и запах озона усилился."
        ),
        known_entities={},
        known_ids=set(),
        acting_character_id=None,
        player_character_id=None,
        scene_participant_ids=[],
    )

    assert len(proposals) == 1
    assert proposals[0].change_type == ChangeType.EVENT
    assert proposals[0].payload["_canon"]["preserved_as"] == "narrative_event"
    assert scribe.last_audit["envelope_valid"] is True


def test_character_claim_gap_preserves_the_act_of_claiming_as_event():
    scribe = MemoryScribe(None)
    proposals = scribe._parse_data(
        {
            "outcomes": [
                {
                    "id": "claim1",
                    "kind": "knowledge_transfer",
                    "description": "Элиас потребовал проверить периметр и соблюдать осторожность.",
                    "evidence": "Элиас потребовал проверить периметр",
                    "authority": "character_claim",
                    "durable": True,
                }
            ],
            "proposals": [],
        },
        authoritative_text="Элиас потребовал проверить периметр и соблюдать осторожность.",
        known_entities={},
        known_ids=set(),
        acting_character_id=None,
        player_character_id=None,
        scene_participant_ids=[],
    )

    assert len(proposals) == 1
    assert proposals[0].change_type == ChangeType.EVENT
    assert proposals[0].payload["_canon"]["authority"] == "character_claim"
    assert scribe.last_audit["envelope_valid"] is True


def test_failed_knowledge_normalization_preserves_evidenced_action_as_event():
    scribe = MemoryScribe(None)
    proposals = scribe._parse_data(
        {
            "outcomes": [
                {
                    "id": "action1",
                    "kind": "knowledge_transfer",
                    "description": "Элдон отметил две точки для дальнейших измерений.",
                    "evidence": "Элдон отметил две точки для дальнейших измерений",
                    "authority": "public_observation",
                    "durable": True,
                }
            ],
            "proposals": [
                {
                    "outcome_id": "action1",
                    "change_type": "knowledge",
                    "operation": "assert",
                    "cardinality": "single",
                    "payload": {
                        "recipient_id": "Неизвестный получатель",
                        "proposition": "Две точки отмечены.",
                    },
                }
            ],
        },
        authoritative_text="Элдон отметил две точки для дальнейших измерений.",
        known_entities={},
        known_ids=set(),
        acting_character_id=None,
        player_character_id=None,
        scene_participant_ids=[],
    )

    assert len(proposals) == 1
    assert proposals[0].change_type == ChangeType.EVENT
    assert proposals[0].payload["_canon"]["preserved_as"] == "narrative_event"
    assert scribe.last_audit["envelope_valid"] is True
    assert scribe.last_audit["gap_count"] == 0


def test_failed_relationship_normalization_preserves_accusation_as_event():
    scribe = MemoryScribe(None)
    proposals = scribe._parse_data(
        {
            "outcomes": [
                {
                    "id": "relation1",
                    "kind": "relationship_change",
                    "description": "Элиас обвинил присутствующих в нарушении порядка.",
                    "evidence": "Элиас обвинил присутствующих в нарушении порядка",
                    "authority": "public_observation",
                    "durable": True,
                }
            ],
            "proposals": [
                {
                    "outcome_id": "relation1",
                    "change_type": "relationship",
                    "operation": "assert",
                    "cardinality": "single",
                    "payload": {
                        "source_id": "Неизвестный Элиас",
                        "target_id": "Неизвестная группа",
                        "relationship_type": "обвинение",
                    },
                }
            ],
        },
        authoritative_text=(
            "Элиас обвинил присутствующих в нарушении порядка."
        ),
        known_entities={},
        known_ids=set(),
        acting_character_id=None,
        player_character_id=None,
        scene_participant_ids=[],
    )

    assert len(proposals) == 1
    assert proposals[0].change_type == ChangeType.EVENT
    assert proposals[0].payload["_canon"]["preserved_as"] == "narrative_event"
    assert scribe.last_audit["envelope_valid"] is True


def test_scribe_guard_preserves_only_evidence_when_description_is_unrelated():
    scribe = MemoryScribe(None)
    proposals = scribe._parse_data(
        {
            "outcomes": [
                {
                    "id": "event1",
                    "kind": "event",
                    "description": "Кристалл вспыхнул и покрылся трещинами.",
                    "evidence": "Мастер Вейн выпрямился и потянулся к поясу.",
                    "authority": "public_observation",
                    "durable": True,
                }
            ],
            "proposals": [
                {
                    "outcome_id": "event1",
                    "change_type": "event",
                    "operation": "assert",
                    "cardinality": "single",
                    "payload": {
                        "event_type": "narrative_event",
                        "description": "Кристалл вспыхнул и покрылся трещинами.",
                        "participant_ids": [],
                    },
                }
            ],
        },
        authoritative_text=(
            "Кристалл вспыхнул и покрылся трещинами. "
            "Мастер Вейн выпрямился и потянулся к поясу."
        ),
        known_entities={},
        known_ids=set(),
        acting_character_id=None,
        player_character_id=None,
        scene_participant_ids=[],
    )

    assert len(proposals) == 1
    assert proposals[0].change_type == ChangeType.EVENT
    assert proposals[0].payload["_canon"]["_evidence_mismatch"] is True
    assert "Мастер Вейн выпрямился" in proposals[0].payload["description"]
    assert "Кристалл вспыхнул" not in proposals[0].payload["description"]
    assert scribe.last_audit["envelope_valid"] is True
    assert scribe.last_audit["gap_count"] == 0


def test_scribe_guard_replaces_singular_player_placeholder_with_eldon():
    scribe = MemoryScribe(None)
    proposals = scribe._parse_data(
        {
            "outcomes": [
                {
                    "id": "knowledge1",
                    "kind": "knowledge_transfer",
                    "description": "Игрок узнает о духовном риске расследования.",
                    "evidence": "Духовный риск расследования состоит в потере рассудка.",
                    "authority": "character_claim",
                    "durable": True,
                }
            ],
            "proposals": [
                {
                    "outcome_id": "knowledge1",
                    "change_type": "knowledge",
                    "operation": "assert",
                    "cardinality": "single",
                    "payload": {
                        "recipient_id": "Элдон",
                        "proposition": "Предложения игрока были отвергнуты.",
                    },
                }
            ],
        },
        authoritative_text=(
            "Духовный риск расследования состоит в потере рассудка."
        ),
        known_entities={"Элдон": "eldon-id"},
        known_ids={"eldon-id"},
        acting_character_id="eldon-id",
        player_character_id="eldon-id",
        scene_participant_ids=["eldon-id"],
    )

    assert len(proposals) == 1
    assert proposals[0].payload["_canon"]["description"].startswith("Элдон узнает")
    assert "игрок" not in str(proposals[0].payload).casefold()
    sanitized = _replace_player_meta_in_value(
        {"proposition": "Предложения игрока были отвергнуты."}
    )
    assert sanitized["proposition"] == "Предложения Элдона были отвергнуты."


def test_scribe_guard_rejects_foreign_script_inside_canon_evidence():
    scribe = MemoryScribe(None)
    proposals = scribe._parse_data(
        {
            "outcomes": [
                {
                    "id": "event1",
                    "kind": "event",
                    "description": "Кира осмотрела кристаллы 她.",
                    "evidence": "Кира осмотрела кристаллы 她",
                    "authority": "public_observation",
                    "durable": True,
                }
            ],
            "proposals": [],
        },
        authoritative_text="Кира осмотрела кристаллы 她.",
        known_entities={},
        known_ids=set(),
        acting_character_id=None,
        player_character_id=None,
        scene_participant_ids=[],
    )

    assert proposals == []
    assert scribe.last_audit["envelope_valid"] is False
    assert scribe.last_audit["gap_count"] == 1
