from app.models.proposed_change import ChangeType, ProposedChangeCreate
from app.services.player_quote_provenance_guard import filter_player_quote_echoes


def _knowledge(evidence: str, authority: str = "character_claim") -> ProposedChangeCreate:
    return ProposedChangeCreate(
        change_type=ChangeType.KNOWLEDGE,
        payload={
            "recipient_id": "00000000-0000-4000-8000-000000000001",
            "source_character_id": "00000000-0000-4000-8000-000000000002",
            "proposition": evidence,
            "_canon": {
                "outcome_id": "quoted-claim-1",
                "evidence": evidence,
                "authority": authority,
            },
        },
    )


def test_player_quote_echo_cannot_become_npc_character_claim() -> None:
    player_input = (
        "Я возвращаю Мартину латунный ключ. "
        "Я полностью выполняю условие нашего долга."
    )
    proposal = _knowledge("Я полностью выполняю условие нашего долга.")

    assert filter_player_quote_echoes([proposal], player_input) == []


def test_distinct_npc_claim_survives_player_echo_filter() -> None:
    player_input = "Я спрашиваю Мартина, кому принадлежит склад."
    proposal = _knowledge("По моему мнению, склад принадлежит компании Север.")

    assert filter_player_quote_echoes([proposal], player_input) == [proposal]


def test_non_character_claim_is_not_touched_by_player_echo_filter() -> None:
    proposal = _knowledge("Я вернул ключ.", authority="public_observation")

    assert filter_player_quote_echoes([proposal], "Я вернул ключ.") == [proposal]
