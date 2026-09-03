from live_model_contracts.inventory_oracles import undo_item_drop_oracle
from live_model_contracts.snapshot import TruthSnapshot
from live_model_contracts.world import FixtureWorld


def _snapshot(*, sequences):
    return TruthSnapshot(
        campaign_id="campaign",
        data={
            "entities": [
                {
                    "id": "key-id",
                    "type": "item",
                    "name": "латунный ключ (Кай)",
                    "owner": "Кай",
                    "location": None,
                }
            ],
            "action_sequences": sequences,
            "turns": [],
        },
    )


def _world():
    return FixtureWorld(
        campaign_id="campaign",
        hero_id="hero",
        scene_id="scene",
        items={"латунный ключ": "key-id"},
    )


def test_undo_item_oracle_uses_fixture_id_and_requires_real_drop_sequence():
    after = _snapshot(
        sequences=[
            {
                "status": "undone",
                "steps": [
                    {
                        "status": "undone",
                        "action_type": "inventory",
                        "item_operation": "drop",
                    }
                ],
            }
        ]
    )

    assert undo_item_drop_oracle(after, after, _world()) == []


def test_undo_item_oracle_rejects_noop_undo_even_when_owner_looks_restored():
    after = _snapshot(sequences=[])

    failures = undo_item_drop_oracle(after, after, _world())

    assert failures == [
        "undo did not prove an applied deterministic inventory drop before compensation"
    ]
