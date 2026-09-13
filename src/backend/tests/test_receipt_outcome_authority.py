from types import SimpleNamespace

import pytest

from app.services.truth_engine_receipts import StructuredReceiptEventCompiler


@pytest.mark.parametrize('status', ['blocked', 'skipped', 'pending'])
def test_noncompleted_receipt_never_publishes_planned_success(status):
    step = SimpleNamespace(status=status, observable_outcome='The traveler arrives.',
                           intent='travel', blocking_reason='The passage is closed.', step_index=0)
    result = StructuredReceiptEventCompiler._observed_step_outcome(step)
    assert 'The traveler arrives.' not in result
    assert 'The passage is closed.' in result
    assert result.startswith(status.upper())


def test_completed_receipt_publishes_observed_outcome():
    step = SimpleNamespace(status='completed', observable_outcome='The lamp is lit.',
                           intent='operate lamp', blocking_reason=None, step_index=0)
    assert StructuredReceiptEventCompiler._observed_step_outcome(step) == 'The lamp is lit.'
