"""Keep remote identities/custody available without repeating every archived document."""
from __future__ import annotations

import re
from functools import lru_cache

from app.models.item_state import ItemStateSnapshot


@lru_cache(maxsize=4096)
def _lemmas(text: str) -> frozenset[str]:
    from app.services.linguistic_intent_analyzer import _russian_morphology

    morphology = _russian_morphology()
    return frozenset(morphology.parse(word)[0].normal_form
                     for word in re.findall(r"[^\W\d_]{3,}", text.casefold()))


def prompt_item_states(states: list[ItemStateSnapshot], *, location_id=None,
                       participant_ids=(), query: str = "") -> list[dict]:
    """Selection affects prompt detail only. All IDs/positions survive; audits use full records."""
    participants = {str(value) for value in participant_ids if value}
    words = _lemmas(query)
    result = []
    for state in states:
        detailed = (
            location_id is not None and str(location_id) in {
                str(state.location_id), str(state.owner_location_id),
            }
            or state.owner_id is not None and str(state.owner_id) in participants
            or str(state.item_id) in query
            or bool(words & _lemmas(state.name))
        )
        snapshot = state.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
        if not detailed:
            if snapshot.pop("revealed_content", None) is not None:
                snapshot["archived_content_available"] = True
            if snapshot.pop("properties", None):
                snapshot["archived_properties_available"] = True
            snapshot.pop("content_source_step_id", None)
        result.append(snapshot)
    return result
