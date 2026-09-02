from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import ClassVar
from uuid import UUID

from sqlalchemy import literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.memory_taxonomy_table import FactMemoryProfile, NarrativeDetail
from app.db.tables import Campaign, Entity, Fact, Scene, SceneThesis, Turn
from app.db.thesis_lifecycle_table import ThesisLifecycleProfile
from app.models.memory_ops import (
    MemoryMaintenanceAction,
    MemoryMaintenanceRequest,
    MemoryMaintenanceResult,
)


class MemoryOperationsService:
    """Inspect and safely maintain working memory without rewriting canon."""

    THESIS_TTL: ClassVar[dict[str, int]] = {
        "canon": 16,
        "intention": 8,
        "relationship_dynamic": 10,
        "secret": 12,
        "tension": 5,
        "unresolved_beat": 8,
        "visual_state": 3,
        "music_mood": 3,
    }
    MAX_ACTIVE_THESES = 10

    def __init__(self, session: AsyncSession):
        self._session = session

    @classmethod
    def default_ttl(cls, thesis_type: str) -> int:
        return cls.THESIS_TTL.get(thesis_type, 8)

    @staticmethod
    def _normalized_text(value: str) -> str:
        value = value.casefold().replace("ё", "е")
        return " ".join(re.findall(r"[\w]+", value, flags=re.UNICODE))

    @classmethod
    def semantic_key(cls, value: str) -> str:
        normalized = cls._normalized_text(value)
        return normalized[:160] or "thesis"

    @staticmethod
    def _related_ids(raw: str | None) -> list[str]:
        if not raw:
            return []
        try:
            return sorted(str(value) for value in json.loads(raw))
        except (TypeError, ValueError, json.JSONDecodeError):
            return []

    @classmethod
    def thesis_scope(cls, thesis: SceneThesis) -> str:
        related = ",".join(cls._related_ids(thesis.related_entity_ids))
        return f"{thesis.thesis_type}:{related or 'scene'}"

    @classmethod
    def thesis_slot(
        cls,
        thesis: SceneThesis,
        profile: ThesisLifecycleProfile | None = None,
    ) -> str:
        semantic = cls.semantic_key(
            profile.semantic_key if profile and profile.semantic_key else thesis.text
        )
        return f"{cls.thesis_scope(thesis)}:{semantic}"

    async def ensure_thesis_profile(
        self,
        thesis: SceneThesis,
        *,
        reinforced_turn_id: UUID | str | None = None,
        semantic_key: str | None = None,
    ) -> ThesisLifecycleProfile:
        row = await self._session.get(ThesisLifecycleProfile, thesis.id)
        if row is None:
            row = ThesisLifecycleProfile(
                thesis_id=thesis.id,
                semantic_key=semantic_key or self.semantic_key(thesis.text),
                ttl_turns=self.default_ttl(thesis.thesis_type),
            )
            self._session.add(row)
        elif semantic_key:
            row.semantic_key = self.semantic_key(semantic_key)
        if reinforced_turn_id:
            row.last_reinforced_turn_id = str(reinforced_turn_id)
            row.closure_reason = None
        await self._session.flush()
        return row

    async def record_reconcile(
        self,
        scene_id: UUID,
        source_turn_id: UUID,
        desired: list,
    ) -> None:
        active = (
            await self._session.execute(
                select(SceneThesis).where(
                    SceneThesis.scene_id == str(scene_id),
                    SceneThesis.status == "active",
                )
            )
        ).scalars().all()

        profiles: dict[str, ThesisLifecycleProfile] = {}
        for thesis in active:
            profiles[thesis.id] = await self.ensure_thesis_profile(thesis)

        desired_by_id: dict[str, object] = {}
        desired_by_slot: dict[str, object] = {}
        for item in desired:
            existing_id = getattr(item, "existing_thesis_id", None)
            if existing_id:
                desired_by_id[str(existing_id)] = item
                continue
            thesis_type = getattr(item.thesis_type, "value", item.thesis_type)
            related = ",".join(sorted(str(value) for value in item.related_entity_ids))
            semantic = self.semantic_key(
                getattr(item, "semantic_key", None) or getattr(item, "text", "")
            )
            desired_by_slot[f"{thesis_type}:{related or 'scene'}:{semantic}"] = item

        for thesis in active:
            profile = profiles[thesis.id]
            item = desired_by_id.get(thesis.id)
            if item is None:
                item = desired_by_slot.get(self.thesis_slot(thesis, profile))
            if item is None and not thesis.pinned:
                continue
            # Existing IDs own their already-persisted semantic slot. A model may repeat a
            # slightly different key, but reinforcement must not silently rename the slot.
            semantic = profile.semantic_key
            if item is not None and not getattr(item, "existing_thesis_id", None):
                semantic = getattr(item, "semantic_key", None) or semantic
            await self.ensure_thesis_profile(
                thesis,
                reinforced_turn_id=source_turn_id,
                semantic_key=semantic,
            )

        rows = (
            await self._session.execute(
                select(SceneThesis, ThesisLifecycleProfile)
                .join(
                    ThesisLifecycleProfile,
                    ThesisLifecycleProfile.thesis_id == SceneThesis.id,
                )
                .where(
                    SceneThesis.scene_id == str(scene_id),
                    SceneThesis.status != "active",
                    ThesisLifecycleProfile.closure_reason.is_(None),
                )
            )
        ).all()
        for thesis, profile in rows:
            profile.closure_reason = thesis.status
        await self._session.flush()

    async def record_closed_scene(self, scene_id: UUID) -> None:
        rows = (
            await self._session.execute(
                select(SceneThesis, ThesisLifecycleProfile)
                .join(
                    ThesisLifecycleProfile,
                    ThesisLifecycleProfile.thesis_id == SceneThesis.id,
                    isouter=True,
                )
                .where(SceneThesis.scene_id == str(scene_id))
            )
        ).all()
        for thesis, profile in rows:
            if profile is None:
                profile = await self.ensure_thesis_profile(thesis)
            if thesis.status != "active":
                profile.closure_reason = profile.closure_reason or "scene_closed"
        await self._session.flush()

    async def _turn_positions(
        self,
        campaign_id: UUID,
    ) -> tuple[dict[str, dict[str, int]], dict[str, int]]:
        turns = (
            await self._session.execute(
                select(Turn)
                .where(
                    Turn.campaign_id == str(campaign_id),
                    Turn.role == "assistant",
                    Turn.status == "active",
                    Turn.scene_id.is_not(None),
                )
                # PersonalDM's durable store is SQLite. UUID text is identity, not chronology:
                # when two turns share one timestamp, SQLite rowid preserves insertion order
                # instead of turning a random UUID into the memory-age tie-breaker.
                .order_by(Turn.created_at, literal_column("turns.rowid"))
            )
        ).scalars().all()
        positions: dict[str, dict[str, int]] = defaultdict(dict)
        latest: dict[str, int] = {}
        for turn in turns:
            index = len(positions[turn.scene_id])
            positions[turn.scene_id][turn.id] = index
            latest[turn.scene_id] = index
        return dict(positions), latest

    @staticmethod
    def _detail_expired(
        detail: NarrativeDetail,
        scene: Scene | None,
        positions: dict[str, dict[str, int]],
        latest: dict[str, int],
    ) -> tuple[bool, str | None, int | None]:
        if scene is None:
            return True, "scene_missing", None
        if scene.status != "active":
            return True, "scene_closed", None
        if not detail.source_turn_id:
            return False, "source_turn_missing", None
        scene_positions = positions.get(detail.scene_id, {})
        source_index = scene_positions.get(detail.source_turn_id)
        if source_index is None:
            return True, "source_turn_not_active", None
        age = latest.get(detail.scene_id, source_index) - source_index
        if age >= detail.turn_window:
            return True, "turn_window_elapsed", age
        return False, None, age

    async def snapshot(self, campaign_id: UUID) -> dict:
        campaign = await self._session.get(Campaign, str(campaign_id))
        if not campaign:
            raise ValueError("Campaign not found")

        entities = (
            await self._session.execute(
                select(Entity).where(Entity.campaign_id == str(campaign_id))
            )
        ).scalars().all()
        entity_names = {row.id: row.canonical_name for row in entities}
        entity_ids = set(entity_names)

        scenes = (
            await self._session.execute(
                select(Scene).where(Scene.campaign_id == str(campaign_id))
            )
        ).scalars().all()
        scene_map = {row.id: row for row in scenes}
        scene_ids = set(scene_map)
        positions, latest = await self._turn_positions(campaign_id)

        facts = (
            await self._session.execute(
                select(Fact, FactMemoryProfile)
                .join(
                    FactMemoryProfile,
                    FactMemoryProfile.fact_id == Fact.id,
                    isouter=True,
                )
                .where(Fact.campaign_id == str(campaign_id))
                .order_by(Fact.created_at)
            )
        ).all()
        fact_items = []
        fact_issues = []
        memory_counts = defaultdict(int)
        for fact, profile in facts:
            kind = profile.memory_kind if profile else None
            inferred = "scene_state" if fact.scope == "scene" else "world_canon"
            memory_counts[kind or "missing_profile"] += 1
            issues = []
            if profile is None:
                issues.append("missing_profile")
            elif kind == "world_canon" and fact.scope != "campaign":
                issues.append("world_canon_has_scene_scope")
            elif kind == "scene_state" and (
                fact.scope != "scene" or not fact.scene_id or fact.scene_id not in scene_ids
            ):
                issues.append("scene_state_has_invalid_scene")
            elif kind == "entity_state" and (
                not profile.subject_entity_id
                or profile.subject_entity_id not in entity_ids
            ):
                issues.append("entity_state_has_invalid_subject")
            for issue in issues:
                fact_issues.append(f"fact {fact.id}: {issue}")
            fact_items.append(
                {
                    "id": fact.id,
                    "subject": fact.subject,
                    "predicate": fact.predicate,
                    "object_value": fact.object_value,
                    "scope": fact.scope,
                    "scene_id": fact.scene_id,
                    "memory_kind": kind,
                    "inferred_kind": inferred,
                    "subject_entity_id": (
                        profile.subject_entity_id if profile else None
                    ),
                    "subject_entity_name": entity_names.get(
                        profile.subject_entity_id if profile else None
                    ),
                    "is_current": fact.is_current,
                    "issues": issues,
                }
            )

        details = (
            await self._session.execute(
                select(NarrativeDetail)
                .where(NarrativeDetail.campaign_id == str(campaign_id))
                .order_by(NarrativeDetail.created_at)
            )
        ).scalars().all()
        detail_items = []
        detail_issues = []
        expired_detail_ids: list[str] = []
        for detail in details:
            expired, reason, age = self._detail_expired(
                detail,
                scene_map.get(detail.scene_id),
                positions,
                latest,
            )
            issues = []
            if reason == "source_turn_missing":
                issues.append("unbounded_without_source_turn")
            if detail.subject_entity_id and detail.subject_entity_id not in entity_ids:
                issues.append("subject_entity_missing")
            if expired:
                expired_detail_ids.append(detail.id)
            for issue in issues:
                detail_issues.append(f"detail {detail.id}: {issue}")
            detail_items.append(
                {
                    "id": detail.id,
                    "scene_id": detail.scene_id,
                    "scene_title": (
                        scene_map[detail.scene_id].title
                        if detail.scene_id in scene_map
                        else None
                    ),
                    "source_turn_id": detail.source_turn_id,
                    "subject_entity_id": detail.subject_entity_id,
                    "subject_entity_name": entity_names.get(detail.subject_entity_id),
                    "detail_type": detail.detail_type,
                    "text": detail.text,
                    "visibility": detail.visibility,
                    "turn_window": detail.turn_window,
                    "age_turns": age,
                    "expired_candidate": expired,
                    "expiry_reason": reason if expired else None,
                    "issues": issues,
                }
            )

        thesis_rows = (
            await self._session.execute(
                select(SceneThesis, ThesisLifecycleProfile)
                .join(
                    ThesisLifecycleProfile,
                    ThesisLifecycleProfile.thesis_id == SceneThesis.id,
                    isouter=True,
                )
                .where(SceneThesis.scene_id.in_(scene_ids or {"__none__"}))
                .order_by(SceneThesis.created_at)
            )
        ).all()
        active_groups: dict[
            tuple[str, str],
            list[tuple[SceneThesis, ThesisLifecycleProfile | None]],
        ] = defaultdict(list)
        active_by_scene: dict[
            str,
            list[tuple[SceneThesis, ThesisLifecycleProfile | None]],
        ] = defaultdict(list)
        for thesis, profile in thesis_rows:
            if thesis.status == "active":
                active_groups[(thesis.scene_id, self.thesis_slot(thesis, profile))].append(
                    (thesis, profile)
                )
                active_by_scene[thesis.scene_id].append((thesis, profile))

        maintenance_reasons: dict[str, str] = {}
        for rows in active_groups.values():
            if len(rows) <= 1:
                continue
            (keeper_thesis, _), *_ = sorted(
                rows,
                key=lambda pair: (
                    int(pair[0].pinned),
                    pair[0].priority,
                    pair[0].updated_at,
                    pair[0].created_at,
                ),
                reverse=True,
            )
            for thesis, _ in rows:
                if thesis.id != keeper_thesis.id:
                    maintenance_reasons[thesis.id] = "duplicate_semantic_slot"

        for rows in active_by_scene.values():
            candidates = [pair for pair in rows if not pair[0].pinned]
            if len(candidates) <= self.MAX_ACTIVE_THESES:
                continue
            keep = {
                thesis.id
                for thesis, _ in sorted(
                    candidates,
                    key=lambda pair: (
                        pair[0].priority,
                        pair[0].updated_at,
                        pair[0].created_at,
                    ),
                    reverse=True,
                )[: self.MAX_ACTIVE_THESES]
            }
            for thesis, _ in candidates:
                if thesis.id not in keep:
                    maintenance_reasons.setdefault(
                        thesis.id,
                        "active_limit_exceeded",
                    )

        thesis_items = []
        thesis_issues = []
        stale_thesis_ids: list[str] = []
        missing_thesis_profiles: list[str] = []
        for thesis, profile in thesis_rows:
            scene = scene_map.get(thesis.scene_id)
            anchor = (
                profile.last_reinforced_turn_id
                if profile and profile.last_reinforced_turn_id
                else thesis.source_turn_id
            )
            age = None
            if anchor and thesis.scene_id in positions:
                anchor_index = positions[thesis.scene_id].get(anchor)
                if anchor_index is not None:
                    age = latest.get(thesis.scene_id, anchor_index) - anchor_index
            ttl = profile.ttl_turns if profile else self.default_ttl(thesis.thesis_type)
            stale_reason = maintenance_reasons.get(thesis.id)
            if thesis.status == "active" and not thesis.pinned:
                if scene is None or scene.status != "active":
                    stale_reason = stale_reason or "scene_closed"
                elif age is not None and age >= ttl:
                    stale_reason = stale_reason or "ttl_elapsed"
            if thesis.status == "active" and profile is None:
                missing_thesis_profiles.append(thesis.id)
                thesis_issues.append(f"thesis {thesis.id}: missing_lifecycle_profile")
            if stale_reason:
                stale_thesis_ids.append(thesis.id)
            thesis_items.append(
                {
                    "id": thesis.id,
                    "scene_id": thesis.scene_id,
                    "scene_title": scene.title if scene else None,
                    "type": thesis.thesis_type,
                    "text": thesis.text,
                    "priority": thesis.priority,
                    "status": thesis.status,
                    "pinned": thesis.pinned,
                    "scope": self.thesis_scope(thesis),
                    "semantic_key": (
                        profile.semantic_key
                        if profile
                        else self.semantic_key(thesis.text)
                    ),
                    "ttl_turns": ttl,
                    "age_turns": age,
                    "last_reinforced_turn_id": (
                        profile.last_reinforced_turn_id if profile else None
                    ),
                    "closure_reason": profile.closure_reason if profile else None,
                    "maintenance_reason": stale_reason,
                }
            )

        return {
            "campaign_id": str(campaign_id),
            "memory_counts": dict(memory_counts),
            "facts": fact_items,
            "narrative_details": detail_items,
            "theses": thesis_items,
            "issues": [*fact_issues, *detail_issues, *thesis_issues],
            "maintenance_candidates": {
                "expired_detail_ids": expired_detail_ids,
                "stale_thesis_ids": stale_thesis_ids,
                "missing_thesis_profile_ids": missing_thesis_profiles,
                "missing_fact_profile_ids": [
                    item["id"]
                    for item in fact_items
                    if "missing_profile" in item["issues"]
                ],
            },
            "health": {
                "memory_profile_errors": len(fact_issues),
                "transient_memory_warnings": len(detail_issues),
                "expired_transient_details": len(expired_detail_ids),
                "thesis_lifecycle_errors": len(thesis_issues),
                "stale_or_duplicate_theses": len(stale_thesis_ids),
            },
        }

    async def maintain(
        self,
        campaign_id: UUID,
        request: MemoryMaintenanceRequest,
    ) -> MemoryMaintenanceResult:
        snapshot = await self.snapshot(campaign_id)
        candidates = snapshot["maintenance_candidates"]
        actions: list[MemoryMaintenanceAction] = []
        details_cleaned = 0
        theses_closed = 0
        profiles_repaired = 0

        if request.repair_missing_profiles:
            for raw_id in candidates["missing_fact_profile_ids"]:
                fact = await self._session.get(Fact, raw_id)
                if fact is None:
                    continue
                action = MemoryMaintenanceAction(
                    action="create_profile",
                    target_type="fact",
                    target_id=UUID(fact.id),
                    reason="missing_profile",
                    changes_data=True,
                )
                actions.append(action)
                if request.apply_changes:
                    self._session.add(
                        FactMemoryProfile(
                            fact_id=fact.id,
                            memory_kind=(
                                "scene_state"
                                if fact.scope == "scene"
                                else "world_canon"
                            ),
                        )
                    )
                    profiles_repaired += 1

            for raw_id in candidates["missing_thesis_profile_ids"]:
                thesis = await self._session.get(SceneThesis, raw_id)
                if thesis is None:
                    continue
                actions.append(
                    MemoryMaintenanceAction(
                        action="create_profile",
                        target_type="thesis",
                        target_id=UUID(thesis.id),
                        reason="missing_lifecycle_profile",
                        changes_data=True,
                    )
                )
                if request.apply_changes:
                    await self.ensure_thesis_profile(thesis)
                    profiles_repaired += 1

        if request.clean_expired_details:
            for raw_id in candidates["expired_detail_ids"]:
                detail = await self._session.get(NarrativeDetail, raw_id)
                if detail is None:
                    continue
                item = next(
                    value
                    for value in snapshot["narrative_details"]
                    if value["id"] == raw_id
                )
                actions.append(
                    MemoryMaintenanceAction(
                        action="remove_expired",
                        target_type="narrative_detail",
                        target_id=UUID(detail.id),
                        reason=item["expiry_reason"] or "expired",
                        changes_data=True,
                    )
                )
                if request.apply_changes:
                    await self._session.delete(detail)
                    details_cleaned += 1

        if request.close_stale_theses:
            for raw_id in candidates["stale_thesis_ids"]:
                thesis = await self._session.get(SceneThesis, raw_id)
                if thesis is None or thesis.pinned or thesis.status != "active":
                    continue
                item = next(
                    value
                    for value in snapshot["theses"]
                    if value["id"] == raw_id
                )
                reason = item["maintenance_reason"] or "stale"
                actions.append(
                    MemoryMaintenanceAction(
                        action="resolve",
                        target_type="scene_thesis",
                        target_id=UUID(thesis.id),
                        reason=reason,
                        changes_data=True,
                    )
                )
                if request.apply_changes:
                    thesis.status = "resolved"
                    profile = await self.ensure_thesis_profile(thesis)
                    profile.closure_reason = reason
                    theses_closed += 1

        if request.apply_changes:
            await self._session.flush()
        return MemoryMaintenanceResult(
            applied=request.apply_changes,
            campaign_id=campaign_id,
            actions=actions,
            details_cleaned=details_cleaned,
            theses_closed=theses_closed,
            profiles_repaired=profiles_repaired,
        )