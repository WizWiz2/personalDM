from tests.simulation_reliable_soak import (
    SCRIPTED_TURNS,
    assess_trace_records,
    contains_session_zero_language,
    scripted_turn_payload,
    technical_publication_findings,
)


def _record(turn: int, *, published: str = "Вы видите мокрую каменную лестницу.") -> dict:
    payload = scripted_turn_payload(
        turn,
        ["Лена", "Трактирщик"],
        "выяснить, кто приходил ночью",
    )
    return {
        "turn": turn,
        "generation_failed": False,
        "player": {**payload, "source": "scripted"},
        "dm": published,
        "narration": {
            "status": "repaired",
            "protocol_status": "repaired",
            "repair_attempts": 1,
            "draft": "[EXECUTED ACTION SEQUENCE] технический черновик",
            "repair": published,
            "published": published,
            "initial_violations": [
                {
                    "violation_type": "player_agency",
                    "severity": "error",
                    "evidence": "черновик",
                    "correction": "убрать",
                }
            ],
            "repair_violations": [],
            "stream_matches_published": True,
            "final_matches_published": True,
        },
    }


def test_fixed_corpus_is_diverse_and_has_no_session_zero_language():
    assert len(SCRIPTED_TURNS) == 30
    payloads = [
        scripted_turn_payload(
            turn,
            ["Лена", "Трактирщик"],
            "выяснить, кто приходил ночью",
        )
        for turn in range(1, 31)
    ]

    assert len({item["intent"] for item in payloads}) == 30
    assert len({item["mode"] for item in payloads}) >= 5
    assert {item["target"] for item in payloads} >= {"narrator"}
    assert all(not contains_session_zero_language(item["intent"]) for item in payloads)


def test_npc_turn_falls_back_to_unique_narrator_action_when_scene_has_no_npc():
    payload = scripted_turn_payload(2, [], "осмотреть пустой склад")

    assert payload["target"] == "narrator"
    assert payload["mode"] == "action"
    assert "Ход 2" in payload["intent"]
    assert not contains_session_zero_language(payload["intent"])


def test_harness_judges_only_published_text_for_technical_leakage():
    records = [_record(turn) for turn in range(1, 31)]

    assessment = assess_trace_records(records, expected_turns=30)

    assert assessment["valid"] is True
    assert assessment["repairs"] == 30
    assert assessment["initial_violations"] == {"player_agency": 30}
    assert assessment["published_technical_leaks"] == []


def test_harness_rejects_session_zero_controller_contamination():
    records = [_record(turn) for turn in range(1, 31)]
    records[6]["player"]["intent"] = "Я уже всё рассказала, начинайте игру."

    assessment = assess_trace_records(records, expected_turns=30)

    assert assessment["valid"] is False
    assert assessment["session_zero_turns"] == [7]
    assert any("session-zero language" in reason for reason in assessment["reasons"])


def test_harness_reports_only_actual_published_technical_text_as_leak():
    text = "[EXECUTED ACTION SEQUENCE] BLOCKED 123e4567-e89b-12d3-a456-426614174000"
    records = [_record(turn) for turn in range(1, 31)]
    records[10]["dm"] = text
    records[10]["narration"]["published"] = text
    records[10]["narration"]["repair"] = text

    assessment = assess_trace_records(records, expected_turns=30)

    assert assessment["valid"] is False
    assert assessment["published_technical_leaks"][0]["turn"] == 11
    assert set(technical_publication_findings(text)) >= {
        "authority_marker",
        "sequence_jargon",
        "uuid",
    }
