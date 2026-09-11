from __future__ import annotations

from app.services.truth_engine_admission import (
    AtomAdmission,
    AtomAdmissionEnvelope,
    admission_failures,
    admission_response_model,
)


def _admission(
    atom_key: str,
    *,
    verdict: str = "admit",
    evidence_quote: str | None = None,
    receipt_effect_ids: list[str] | None = None,
) -> AtomAdmission:
    return AtomAdmission(
        atom_key=atom_key,
        verdict=verdict,
        evidence_quote=evidence_quote,
        receipt_effect_ids=receipt_effect_ids or [],
    )


def test_exact_narration_quote_admits_existing_atom() -> None:
    failures = admission_failures(
        [
            _admission(
                "f_state",
                evidence_quote="Лампа теперь горит ровным белым светом.",
            )
        ],
        {"f_state"},
        "Щёлкнул выключатель. Лампа теперь горит ровным белым светом.",
        set(),
    )

    assert failures == {}


def test_valid_receipt_effect_can_ground_admission() -> None:
    failures = admission_failures(
        [_admission("r_owner", receipt_effect_ids=["effect-1"])],
        {"r_owner"},
        "Передача завершена.",
        {"effect-1", "effect-2"},
    )

    assert failures == {}


def test_unknown_receipt_effect_fails_closed() -> None:
    failures = admission_failures(
        [_admission("r_owner", receipt_effect_ids=["invented"])],
        {"r_owner"},
        "Передача завершена.",
        {"effect-1"},
    )

    assert "unknown receipt effect" in failures["r_owner"].casefold()


def test_missing_duplicate_and_invented_atom_keys_fail_closed() -> None:
    missing = admission_failures([], {"f1"}, "Состояние изменилось.", set())
    assert "omitted or duplicated" in missing["f1"].casefold()

    duplicate = admission_failures(
        [
            _admission("f1", evidence_quote="Состояние изменилось."),
            _admission("f1", evidence_quote="Состояние изменилось."),
        ],
        {"f1"},
        "Состояние изменилось.",
        set(),
    )
    assert "omitted or duplicated" in duplicate["f1"].casefold()

    invented = admission_failures(
        [
            _admission("f1", evidence_quote="Состояние изменилось."),
            _admission("made_up", evidence_quote="Состояние изменилось."),
        ],
        {"f1"},
        "Состояние изменилось.",
        set(),
    )
    assert "invented atom keys" in invented["f1"].casefold()


def test_reject_or_ungrounded_admit_fails_closed() -> None:
    rejected = admission_failures(
        [_admission("f1", verdict="reject")],
        {"f1"},
        "Состояние изменилось.",
        set(),
    )
    assert "rejected" in rejected["f1"].casefold()

    ungrounded = admission_failures(
        [_admission("f1", evidence_quote="Этого текста нет")],
        {"f1"},
        "Состояние изменилось.",
        set(),
    )
    assert "no exact narration quote" in ungrounded["f1"].casefold()


def test_response_model_is_stable_for_dynamic_receipt_sets() -> None:
    model = admission_response_model({"effect-a", "effect-b"})
    parsed = model.model_validate(
        {
            "admissions": [
                {
                    "atom_key": "f1",
                    "verdict": "admit",
                    "receipt_effect_ids": ["effect-a"],
                }
            ]
        }
    )

    assert isinstance(parsed, AtomAdmissionEnvelope)
