from __future__ import annotations

from functools import wraps
from uuid import UUID

from app.db.repositories.location_repo import LocationRepository
from app.models.location import LocationUpdate
from app.services.location_identity import display_location_name, same_location_reference
from app.services.scene_transition_executor import SceneTransitionExecutor
from app.services.turn_authority_planner import TurnAuthorityPlanner
from app.services.turn_planner import TurnPlanningError
from app.services.turn_saga import TurnSaga

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
- Every proposed `location_transition` should carry a durable destination profile in bridge_summary,
  beginning with `{_PROFILE_MARKER}` and followed by `{_TRANSITION_MARKER}`.
- Require repair when a newly introduced destination lacks that profile, or when a supplied profile
  is role/label-only, mostly about the current movement instead of the place, contains hidden
  information, or is too vague to identify the environment on a later revisit.
- Revisiting an already-known durable location does not require the Planner to restate its complete
  profile; existing canonical place facts remain authoritative.
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

    # Backward-compatible acceptance for durable bridge summaries created before the explicit
    # marker protocol. New Planner output always uses the markers.
    clean = " ".join(text.split()).strip()
    return clean if len(clean) >= 80 else None


def _usable_profile(value: str | None) -> bool:
    if not value or len(value) < 80:
        return False
    words = value.split()
    return len(words) >= 10 and len(set(word.casefold() for word in words)) >= 8


def _location_transitions(plan) -> list:
    result = []
    top = getattr(plan, "scene_transition", None)
    if top and top.required and top.transition_type == "location_transition":
        result.append(top)
    sequence = getattr(plan, "action_sequence", None)
    for step in getattr(sequence, "steps", []) or []:
        transition = getattr(step, "transition", None)
        if (
            transition
            and transition.required
            and transition.transition_type == "location_transition"
        ):
            result.append(transition)
    return result


def _matches_existing_location(destination: str, locations) -> bool:
    clean = " ".join(destination.split())
    display = display_location_name(clean)
    for location in locations:
        candidates = [
            location.canonical_name,
            display_location_name(location.canonical_name),
            *location.aliases,
        ]
        if any(
            same_location_reference(needle, candidate)
            for needle in (clean, display)
            for candidate in candidates
            if candidate
        ):
            return True
    return False


async def _require_gameplay_profiles(session, campaign_id: UUID, plan) -> None:
    """Refuse only the corruption case: materializing a brand-new empty location.

    Planner/reviewer still request profiles for every location transition because a revisit may enrich
    legacy sparse data. Runtime blocking is narrower: an already-known Location is valid authority
    even when the Planner does not redundantly restate its card, while a new destination may not be
    created from a label alone.
    """
    transitions = _location_transitions(plan)
    if not transitions:
        return
    locations = await LocationRepository(session).list_by_campaign(campaign_id)
    for transition in transitions:
        profile = extract_destination_profile(transition.bridge_summary)
        if _usable_profile(profile):
            continue
        destination = transition.destination_location or "destination"
        if transition.destination_location and _matches_existing_location(
            transition.destination_location,
            locations,
        ):
            continue
        raise TurnPlanningError(
            f"New location transition to {destination!r} has no durable public destination profile"
        )


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    if _LOCATION_PLANNING_CONTRACT not in TurnAuthorityPlanner.AUTHORITY_ADDENDUM:
        TurnAuthorityPlanner.AUTHORITY_ADDENDUM += _LOCATION_PLANNING_CONTRACT
    if _LOCATION_REVIEW_CONTRACT not in TurnAuthorityPlanner.SEMANTIC_REVIEW_PROMPT:
        TurnAuthorityPlanner.SEMANTIC_REVIEW_PROMPT += _LOCATION_REVIEW_CONTRACT

    # Product-level completeness belongs at the gameplay planning boundary, not inside the generic
    # low-level transition executor. Direct/admin/replay executor callers therefore keep their
    # original semantics, while every normal player turn fails before mutation if it would create a
    # brand-new destination without a durable public profile.
    original_plan = TurnSaga._plan

    @wraps(original_plan)
    async def profiled_plan(self, *args, **kwargs):
        plan, metadata = await original_plan(self, *args, **kwargs)
        campaign_id = kwargs.get("campaign_id")
        if metadata.get("status") == "completed" and campaign_id is not None:
            await _require_gameplay_profiles(self._session, campaign_id, plan)
        return plan, metadata

    TurnSaga._plan = profiled_plan

    original_apply = SceneTransitionExecutor.apply

    @wraps(original_apply)
    async def profiled_apply(self, campaign_id, source_scene_id, trigger_turn_id, plan, *args, **kwargs):
        location_transition = plan.required and plan.transition_type == "location_transition"
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
        if not _usable_profile(location.description) and _usable_profile(profile):
            custom = dict(location.custom_fields or {})
            custom["profile_source"] = "turn_planner_destination_profile"
            await self._locations.update(
                result.target_location_id,
                LocationUpdate(
                    description=profile,
                    custom_fields=custom,
                ),
            )

        return result

    SceneTransitionExecutor.apply = profiled_apply


__all__ = [
    "_require_gameplay_profiles",
    "extract_destination_profile",
    "install",
]
