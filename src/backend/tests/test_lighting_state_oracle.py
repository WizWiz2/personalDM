import pytest

from live_model_contracts.state_oracles import light_is_on


@pytest.mark.parametrize(
    "predicate, expected",
    [
        ("загорается", True),
        ("загорелась", True),
        ("не загорается", False),
        ("не загорелась", False),
    ],
)
def test_lamp_ignition_is_an_on_state_unless_negated(predicate, expected):
    assert (
        light_is_on(
            {
                "subject": "лампа над головой Кая",
                "predicate": predicate,
                "object": "ровным желтоватым светом",
            }
        )
        is expected
    )
