import pytest
from app.services.discourse_evidence import exclusively_quoted


@pytest.mark.parametrize('source', ['Speaker: «Naya works elsewhere».', 'Speaker: "Naya works elsewhere".',
                                   'Speaker: «The note says “Naya works elsewhere”».'])
def test_quote_only_occurrence_is_a_discourse_reference(source):
    assert exclusively_quoted(source, 'Naya')


@pytest.mark.parametrize('source', ['Naya says: «I am here».', 'Naya arrives. Speaker: «Naya».',
                                   'Speaker: «Nayara».', 'Someone else arrives.'])
def test_no_inference_from_absence_or_nonquoted_occurrence(source):
    assert not exclusively_quoted(source, 'Naya')
