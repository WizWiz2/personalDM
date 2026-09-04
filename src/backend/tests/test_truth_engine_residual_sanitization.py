from app.models.truth_engine_residual import SemanticResidualEnvelope


def test_raw_model_output_rekeys_duplicate_atom_keys_by_content():
    envelope = SemanticResidualEnvelope.model_validate(
        {
            "entities": [
                {"ref": "lamp", "mention_text": "lamp", "entity_type": "item"},
            ],
            "fluents": [
                {
                    "atom_key": "state",
                    "subject_ref": "lamp",
                    "semantic_description": "current light state",
                    "value": "on",
                    "description": "The lamp is on.",
                },
                {
                    "atom_key": "state",
                    "subject_ref": "lamp",
                    "semantic_description": "current temperature",
                    "value": "warm",
                    "description": "The lamp is warm.",
                },
            ],
        }
    )

    assert len(envelope.fluents) == 2
    assert envelope.fluents[0].atom_key.startswith("f_")
    assert envelope.fluents[1].atom_key.startswith("f_")
    assert envelope.fluents[0].atom_key != envelope.fluents[1].atom_key


def test_dangling_relation_drops_only_that_atom_from_raw_model_output():
    envelope = SemanticResidualEnvelope.model_validate(
        {
            "entities": [
                {"ref": "lamp", "mention_text": "lamp", "entity_type": "item"},
            ],
            "fluents": [
                {
                    "atom_key": "lit",
                    "subject_ref": "lamp",
                    "semantic_description": "current light state",
                    "value": True,
                    "description": "The lamp is lit.",
                }
            ],
            "relations": [
                {
                    "atom_key": "lights",
                    "subject_ref": "lamp",
                    "object_ref": "missing_room",
                    "semantic_description": "lights the room",
                    "present": True,
                    "description": "The lamp lights the room.",
                }
            ],
        }
    )

    assert len(envelope.fluents) == 1
    assert envelope.relations == []


def test_exact_duplicate_atoms_are_deduplicated_after_rekeying():
    atom = {
        "atom_key": "duplicate",
        "subject_ref": "lamp",
        "semantic_description": "current light state",
        "value": True,
        "description": "The lamp is lit.",
    }
    envelope = SemanticResidualEnvelope.model_validate(
        {
            "entities": [
                {"ref": "lamp", "mention_text": "lamp", "entity_type": "item"},
            ],
            "fluents": [atom, dict(atom)],
        }
    )

    assert len(envelope.fluents) == 1
