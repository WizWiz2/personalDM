from __future__ import annotations

import re
from functools import wraps

from app.models.location import LocationUpdate
from app.services.scene_transition_executor import SceneTransitionExecutor
from app.services.turn_authority_planner import TurnAuthorityPlanner
from app.services.turn_planner import TurnPlanningError

_INSTALLED = False
_PROFILE_MARKER = "DESTINATION PROFILE:"
_TRANSITION_MARKER = "TRANSITION:"

_LOCATION_PLANNING_CONTRACT = f"""

[LOCATION PROFILE CONTRACT]
A durable location is a world entity, not merely a destination label.
For EVERY `location_transition`, `bridge_summary` must contain two explicit sections:
`{_PROFILE_MARKER} <stable public description>`
`{_TRANSITION_MARKER} <durable result of crossing this boundary>`

The destination profile is player-visible canon. Make it 2-4 concise sentences (at least 80
characters) describing what kind of place this is, its recognizable physical/environmental features
and its ordinary function/feel. It must be useful later when the player revisits the place and for a
future illustration prompt. Do not put hidden secrets, undiscovered threats, the protagonist's
feelings, or one-turn actions into the profile. Do not use generic filler such as "a location" or
repeat only the destination name. Existing canonical place facts outrank new description text.
"""

_LOCATION_REVIEW_CONTRACT = f"""

LOCATION PROFILE REVIEW:
- Every proposed `location_transition` must carry a durable destination profile in bridge_summary,
  beginning with `{_PROFILE_MARKER}` and followed by `{_TRANSITION_MARKER}`.
- Require repair when the profile is missing, role/label-only, mostly about the current movement
  instead of the place, contains hidden information, or is too vague to identify the environment on
  a later revisit.
"""


def extract_destination_profile(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    folded = text.casefold()
    marker_index = folded.find(_PROFILE_MARKER.casefold())
    if marker_index >= 0:
        start = marker_index + len(_PROFILE_MARKER)
        tail = text[start:]
        transition_index = tail.casefold().find(_TRANSITION_MARKER.casefold())
        if transition_index >= 0:
            tail = tail[:transition_index]
        clean = " ".join(tail.split()).strip(" -—–;,.\n\t")
        return clean or None

    # Backward-compatible acceptance for older planner/test fixtures that already supplied a
    # substantial stable bridge summary before the explicit marker protocol existed. New model
    # prompts always use the marker; this path prevents existing good data from becoming unusable.
    clean = " ".join(text.split()).strip()
    return clean if len(clean) >= 80 else None


def _usable_profile(value: str | None) -> bool:
    if not value or len(value) < 80:
        return False
    words = value.split()
    return len(words) >= 10 and len(set(word.casefold() for word in words)) >= 8


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    if _LOCATION_PLANNING_CONTRACT not in TurnAuthorityPlanner.AUTHORITY_ADDENDUM:
        TurnAuthorityPlanner.AUTHORITY_ADDENDUM += _LOCATION_PLANNING_CONTRACT
    if _LOCATION_REVIEW_CONTRACT not in TurnAuthorityPlanner.SEMANTIC_REVIEW_PROMPT:
        TurnAuthorityPlanner.SEMANTIC_REVIEW_PROMPT += _LOCATION_REVIEW_CONTRACT

    original_apply = SceneTransitionExecutor.apply

    @wraps(original_apply)
    async def profiled_apply(self, campaign_id, source_scene_id, trigger_turn_id, plan, *args, **kwargs):
        location_transition = (
            plan.required and plan.transition_type == "location_transition"
        )
        before_ids = set()
        if location_transition:
            before_ids = {
                item.id for item in await self._locations.list_by_campaign(campaign_id)
            }

        result = await original_apply(
            self,
            campaign_id,
            source_scene_id,
            trigger_turn_id,
            plan,
            *args,
            **kwargs,
        )
        if not location_transition or result is None or result.target_location_id is None:
            return result

        location = await self._locations.get_by_id(result.target_location_id)
        if location is None:
            raise TurnPlanningError("Structured transition produced no durable destination location")

        profile = extract_destination_profile(plan.bridge_summary)
        is_new = result.target_location_id not in before_ids
        needs_profile = not _usable_profile(location.description)

        if needs_profile and _usable_profile(profile):
            custom = dict(location.custom_fields or {})
            custom["profile_source"] = "turn_planner_destination_profile"
            await self._locations.update(
                result.target_location_id,
                LocationUpdate(
                    description=profile,
                    custom_fields=custom,
                ),
            )
        elif is_new and needs_profile:
            # This is still inside TurnSaga's prepare transaction. Raising here rolls the new
            # location/scene back instead of committing a permanently empty world-card entity.
            raise TurnPlanningError(
                "New location lacks a durable public destination profile; refusing empty materialization"
            )

        return result

    SceneTransitionExecutor.apply = profiled_apply


__all__ = ["extract_destination_profile", "install"]
