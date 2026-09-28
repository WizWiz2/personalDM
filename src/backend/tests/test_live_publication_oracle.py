import pytest

from live_model_contracts.cases import _no_dead_surface
from live_model_contracts.snapshot import TruthSnapshot
from live_model_contracts.transition_cases import _no_dead_surface as transition_surface


@pytest.mark.parametrize("oracle", [_no_dead_surface, transition_surface])
def test_corner_description_is_not_a_dead_turn_but_entire_stub_is(oracle):
    def check(content):
        failures = []
        snapshot = TruthSnapshot("test", {"turns": [
            {"role": "assistant", "status": "active", "content": content},
        ]})
        oracle(snapshot, failures)
        return failures

    assert check("Мартин смотрит в угол, где ничего не происходит, и отвечает: я знаю Лидию.") == []
    assert check("Пока ничего заметно не меняется.")
    assert check("")
