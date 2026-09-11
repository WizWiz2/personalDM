from __future__ import annotations

import re

from app.models.proposed_change import ChangeType, ProposedChangeCreate

_INSTALLED = False
_WORD_RE = re.compile(r"[\w-]+", flags=re.UNICODE)


def _normalize(value: object) -> str:
    text = " ".join(str(value or "").casefold().replace("ё", "е").split())
    return " ".join(_WORD_RE.findall(text))


def _surface_matches_player(evidence: object, user_content: object) -> bool:
    evidence_key = _normalize(evidence)
    user_key = _normalize(user_content)
    if not evidence_key or not user_key:
        return False
    if evidence_key == user_key:
        return True
    return len(evidence_key) >= 12 and evidence_key in user_key


def _is_player_echo_claim(proposal: ProposedChangeCreate, user_content: str) -> bool:
    if proposal.change_type != ChangeType.KNOWLEDGE:
        return False
    payload = proposal.payload if isinstance(proposal.payload, dict) else {}
    canon = payload.get("_canon") if isinstance(payload.get("_canon"), dict) else {}
    if str(canon.get("authority") or "") != "character_claim":
        return False
    return _surface_matches_player(canon.get("evidence"), user_content)


def filter_player_quote_echoes(
    proposals: list[ProposedChangeCreate],
    user_content: str,
) -> list[ProposedChangeCreate]:
    """Remove narrator-echoed player speech accidentally attributed to an NPC.

    On narrator-managed turns the user's own text is never evidence that an NPC knows or asserted
    something. If Narrator quotes the player verbatim, that published echo remains presentation, not
    a new character claim. Exact/subspan comparison is intentionally conservative and semantic-free.
    """
    return [
        proposal
        for proposal in proposals
        if not _is_player_echo_claim(proposal, user_content)
    ]


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    from app.services.memory_scribe import MemoryScribe

    original_extract = MemoryScribe.extract_proposals

    async def provenance_safe_extract(
        self,
        campaign_id,
        scene_id,
        user_content,
        assistant_content,
        acting_character_id=None,
        player_character_id=None,
    ):
        proposals = await original_extract(
            self,
            campaign_id,
            scene_id,
            user_content,
            assistant_content,
            acting_character_id=acting_character_id,
            player_character_id=player_character_id,
        )
        if acting_character_id is not None:
            return proposals
        return filter_player_quote_echoes(proposals, user_content)

    MemoryScribe.extract_proposals = provenance_safe_extract
    _INSTALLED = True


__all__ = ["filter_player_quote_echoes", "install"]
