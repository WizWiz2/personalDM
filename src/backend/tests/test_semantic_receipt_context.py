from app.services.semantic_receipt_context import memory_evidence


def test_memory_evidence_keeps_published_text_and_exact_receipt_description() -> None:
    text = memory_evidence(
        "Кай щёлкает выключателем.",
        [
            {
                "description": "Свет в комнате включается, освещая пространство.",
                "payload": {
                    "status": "completed",
                    "action_type": "interaction",
                    "observable_outcome": "Свет в комнате включается, освещая пространство.",
                },
            }
        ],
    )

    assert "Кай щёлкает выключателем." in text
    assert "Свет в комнате включается, освещая пространство." in text
    assert '"status": "completed"' in text
    assert '"action_type": "interaction"' in text


def test_memory_evidence_tolerates_empty_receipts() -> None:
    assert memory_evidence("Тихая комната.", []) == "Тихая комната."
