from __future__ import annotations

import json
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.scene_bridge_table import SceneBridge
from app.models.scene_bridge import SceneBridgeRead
from app.services.scene_state_service import SceneStateService


class SceneBridgeService:
    """Create the compact, explicit hand-off between two structured scenes."""

    def __init__(self, session: AsyncSession):
        self._session = session
        self._state = SceneStateService(session)

    async def create_for_transition(
        self,
        campaign_id: UUID,
        transition_id: UUID,
        source_scene_id: UUID | None,
        target_scene_id: UUID,
        *,
        reason: str | None,
        requested_summary: str | None = None,
        carryover_goals: list[str] | None = None,
        unresolved_threads: list[str] | None = None,
    ) -> SceneBridgeRead:
        existing = (
            await self._session.execute(
                select(SceneBridge).where(
                    SceneBridge.transition_id == str(transition_id)
                )
            )
        ).scalar_one_or_none()
        if existing:
            return self._to_read(existing)

        source_state = None
        if source_scene_id:
            source_state = await self._state.get(campaign_id, source_scene_id)
        target_state = await self._state.get(campaign_id, target_scene_id)

        source_names = (
            dict(zip(source_state.participant_ids, source_state.participant_names))
            if source_state
            else {}
        )
        target_names = dict(
            zip(target_state.participant_ids, target_state.participant_names)
        )
        departed_ids = [
            participant_id
            for participant_id in source_names
            if participant_id not in target_names
        ]
        carried_ids = [
            participant_id
            for participant_id in target_names
            if participant_id in source_names
        ]

        summary = requested_summary or self._default_summary(
            source_state,
            target_state,
            reason,
        )
        goals = self._unique(
            [
                *(carryover_goals or []),
                *(
                    [source_state.scene_goal]
                    if source_state and source_state.scene_goal
                    else []
                ),
            ]
        )
        threads = self._unique(
            [
                *(unresolved_threads or []),
                *(
                    [source_state.active_conflict]
                    if source_state
                    and source_state.active_conflict
                    and source_state.active_conflict.casefold() not in {"none", "нет"}
                    else []
                ),
            ]
        )

        source_location = (
            " > ".join(source_state.location_path)
            if source_state and source_state.location_path
            else "the previous scene"
        )
        target_location = (
            " > ".join(target_state.location_path)
            if target_state.location_path
            else "the new scene"
        )
        negative_facts = [
            f"{source_names[participant_id]} remained at {source_location} and is not "
            f"present at {target_location}."
            for participant_id in departed_ids
        ]

        row = SceneBridge(
            campaign_id=str(campaign_id),
            transition_id=str(transition_id),
            source_scene_id=(str(source_scene_id) if source_scene_id else None),
            target_scene_id=str(target_scene_id),
            status="prepared",
            previous_scene_summary=summary,
            carried_goals=json.dumps(goals, ensure_ascii=False),
            unresolved_threads=json.dumps(threads, ensure_ascii=False),
            departed_participant_ids=json.dumps(
                [str(value) for value in departed_ids]
            ),
            departed_participant_names=json.dumps(
                [source_names[value] for value in departed_ids],
                ensure_ascii=False,
            ),
            carried_participant_ids=json.dumps(
                [str(value) for value in carried_ids]
            ),
            carried_participant_names=json.dumps(
                [target_names[value] for value in carried_ids],
                ensure_ascii=False,
            ),
            negative_placement_facts=json.dumps(
                negative_facts,
                ensure_ascii=False,
            ),
        )
        self._session.add(row)
        await self._session.flush()
        return self._to_read(row)

    async def get_for_target_scene(
        self,
        campaign_id: UUID,
        target_scene_id: UUID,
    ) -> SceneBridgeRead | None:
        row = (
            await self._session.execute(
                select(SceneBridge)
                .where(
                    SceneBridge.campaign_id == str(campaign_id),
                    SceneBridge.target_scene_id == str(target_scene_id),
                    SceneBridge.status.in_(("prepared", "applied")),
                )
                .order_by(SceneBridge.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return self._to_read(row) if row else None

    async def mark_status(self, transition_id: UUID, status: str) -> bool:
        row = (
            await self._session.execute(
                select(SceneBridge).where(
                    SceneBridge.transition_id == str(transition_id)
                )
            )
        ).scalar_one_or_none()
        if not row:
            return False
        row.status = status
        now = datetime.utcnow()
        if status == "applied":
            row.applied_at = now
        elif status in {"rolled_back", "undone"}:
            row.undone_at = now
        await self._session.flush()
        return True

    @staticmethod
    def prompt_contract(bridge: SceneBridgeRead) -> str:
        goals = "\n".join(f"- {item}" for item in bridge.carried_goals) or "- none"
        threads = (
            "\n".join(f"- {item}" for item in bridge.unresolved_threads)
            or "- none"
        )
        departed = (
            "\n".join(f"- {item}" for item in bridge.negative_placement_facts)
            or "- nobody explicitly left behind"
        )
        carried = ", ".join(bridge.carried_participant_names) or "none"
        return (
            "[SCENE BRIDGE]\n"
            f"Previous scene summary: {bridge.previous_scene_summary}\n"
            f"Characters carried into this scene: {carried}\n"
            "Goals carried forward:\n"
            f"{goals}\n"
            "Unresolved threads carried forward:\n"
            f"{threads}\n"
            "Explicit negative placement facts:\n"
            f"{departed}\n"
            "Hard rules: this bridge is the only active hand-off from the previous scene. "
            "Do not import its full cast, mood, objects, or incidental details. A character "
            "listed as left behind remains absent until a later structured movement explicitly "
            "brings them into the current scene.\n"
        )

    @staticmethod
    def _default_summary(source_state, target_state, reason: str | None) -> str:
        if not source_state:
            return f"The campaign entered {target_state.scene_title}."
        source_location = " > ".join(source_state.location_path) or "an unspecified location"
        target_location = " > ".join(target_state.location_path) or "an unspecified location"
        parts = [
            f"Scene '{source_state.scene_title}' at {source_location} ended",
            f"and the focus moved to '{target_state.scene_title}' at {target_location}",
        ]
        if reason:
            parts.append(f"because {reason.rstrip('.')}.")
        else:
            parts[-1] += "."
        if source_state.world_time_label:
            parts.append(f"Previous world time: {source_state.world_time_label}.")
        return " ".join(parts)

    @staticmethod
    def _unique(values: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for raw in values:
            value = " ".join(str(raw).split())
            if not value:
                continue
            key = value.casefold()
            if key in seen:
                continue
            seen.add(key)
            result.append(value)
        return result

    @staticmethod
    def _json_list(value: str | None) -> list:
        if not value:
            return []
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, list) else []
        except (TypeError, json.JSONDecodeError):
            return []

    def _to_read(self, row: SceneBridge) -> SceneBridgeRead:
        return SceneBridgeRead(
            id=UUID(row.id),
            campaign_id=UUID(row.campaign_id),
            transition_id=UUID(row.transition_id),
            source_scene_id=(UUID(row.source_scene_id) if row.source_scene_id else None),
            target_scene_id=UUID(row.target_scene_id),
            status=row.status,
            previous_scene_summary=row.previous_scene_summary,
            carried_goals=self._json_list(row.carried_goals),
            unresolved_threads=self._json_list(row.unresolved_threads),
            departed_participant_ids=[
                UUID(value) for value in self._json_list(row.departed_participant_ids)
            ],
            departed_participant_names=self._json_list(
                row.departed_participant_names
            ),
            carried_participant_ids=[
                UUID(value) for value in self._json_list(row.carried_participant_ids)
            ],
            carried_participant_names=self._json_list(
                row.carried_participant_names
            ),
            negative_placement_facts=self._json_list(
                row.negative_placement_facts
            ),
            created_at=row.created_at,
            updated_at=row.updated_at,
            applied_at=row.applied_at,
            undone_at=row.undone_at,
        )
